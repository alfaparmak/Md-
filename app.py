from flask import Flask, render_template, request, redirect, url_for, flash, send_from_directory, jsonify, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime
import os
from PIL import Image
from moviepy import VideoFileClip
import json
import qrcode
from io import BytesIO
import secrets
import base64

app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///digital_signage.db'
app.config['UPLOAD_FOLDER'] = 'static/uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max file size
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'mp4', 'mov'}

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Models
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(20), nullable=False)  # 'admin' or 'superuser'
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    screens = db.relationship('Screen', backref='creator', lazy=True)
    media = db.relationship('Media', backref='uploader', lazy=True)

class Screen(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    location = db.Column(db.String(200))
    status = db.Column(db.String(20), default='active')  # active, inactive, maintenance
    display_token = db.Column(db.String(64), unique=True, nullable=False)
    pairing_code = db.Column(db.String(32), unique=True)
    paired_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    screen_media = db.relationship('ScreenMedia', backref='screen', lazy=True, cascade='all, delete-orphan')

class Media(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(200), nullable=False)
    original_filename = db.Column(db.String(200), nullable=False)
    media_type = db.Column(db.String(10))  # 'image' or 'video'
    rotation = db.Column(db.Integer, default=0)  # 0, 90, 180, 270
    duration = db.Column(db.Integer)  # duration in seconds for images, auto-set for videos
    file_size = db.Column(db.Integer)  # in bytes
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)
    uploaded_by = db.Column(db.Integer, db.ForeignKey('user.id'))
    screen_id = db.Column(db.Integer, db.ForeignKey('screen.id'))
    screen_media = db.relationship('ScreenMedia', backref='media', lazy=True, cascade='all, delete-orphan')

class ScreenMedia(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    screen_id = db.Column(db.Integer, db.ForeignKey('screen.id'), nullable=False)
    media_id = db.Column(db.Integer, db.ForeignKey('media.id'), nullable=False)
    start_time = db.Column(db.DateTime, nullable=False)
    end_time = db.Column(db.DateTime, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), default='scheduled')  # scheduled, active, completed, cancelled

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# QR Code Generator
@app.route('/qr/<token>')
def get_qr_code(token):
    display_url = url_for('display_screen', token=token, _external=True)
    
    # Generate QR code
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(display_url)
    qr.make(fit=True)

    # Create image
    img = qr.make_image(fill_color="black", back_color="white")
    
    # Save to BytesIO
    img_io = BytesIO()
    img.save(img_io, 'PNG')
    img_io.seek(0)
    
    return send_file(img_io, mimetype='image/png')

@app.route('/')
@login_required
def index():
    if current_user.role == 'superuser':
        screens = Screen.query.all()
    else:
        screens = Screen.query.filter_by(created_by=current_user.id).all()
    return render_template('index.html', screens=screens)

@app.route('/connect')
def connect_screen():
    return render_template('connect_screen.html')

@app.route('/screen/generate-qr', methods=['POST'])
def generate_screen_qr():
    # Generate a unique pairing code
    pairing_code = secrets.token_hex(16)
    
    # Create a new screen with the pairing code
    screen = Screen(
        name=f"Screen_{pairing_code[:8]}",
        display_token=secrets.token_hex(32),
        pairing_code=pairing_code,
        status='inactive'
    )
    db.session.add(screen)
    db.session.commit()
    
    # Generate QR code
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(pairing_code)
    qr.make(fit=True)
    
    # Create QR image
    img = qr.make_image(fill_color="black", back_color="white")
    
    # Convert to base64
    img_io = BytesIO()
    img.save(img_io, 'PNG')
    img_io.seek(0)
    img_base64 = base64.b64encode(img_io.getvalue()).decode()
    
    return jsonify({
        'success': True,
        'code': pairing_code,
        'qr_url': f"data:image/png;base64,{img_base64}"
    })

@app.route('/screen/check-pairing/<code>')
def check_screen_pairing(code):
    screen = Screen.query.filter_by(pairing_code=code).first()
    
    if not screen:
        return jsonify({'error': 'Invalid pairing code'}), 404
    
    if screen.paired_at:
        return jsonify({
            'paired': True,
            'display_url': url_for('display_screen', token=screen.display_token, _external=True)
        })
    
    return jsonify({'paired': False})

@app.route('/screen/pair', methods=['POST'])
def pair_screen():
    data = request.json
    pairing_code = data.get('code')
    screen_name = data.get('name')
    location = data.get('location')
    
    if not pairing_code:
        return jsonify({'error': 'Pairing code is required'}), 400
    
    screen = Screen.query.filter_by(pairing_code=pairing_code).first()
    if not screen:
        return jsonify({'error': 'Invalid pairing code'}), 404
    
    if screen.paired_at:
        return jsonify({'error': 'Screen is already paired'}), 400
    
    # Update screen information
    screen.name = screen_name or screen.name
    screen.location = location
    screen.status = 'active'
    screen.paired_at = datetime.utcnow()
    screen.created_by = current_user.id if current_user.is_authenticated else None
    
    db.session.commit()
    
    return jsonify({
        'success': True,
        'display_url': url_for('display_screen', token=screen.display_token, _external=True)
    })

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for('index'))
        
        flash('Invalid username or password')
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/screen/add', methods=['GET', 'POST'])
@login_required
def add_screen():
    if current_user.role not in ['admin', 'superuser']:
        flash('Permission denied')
        return redirect(url_for('index'))

    if request.method == 'POST':
        name = request.form.get('name')
        location = request.form.get('location')
        
        if name:
            import secrets
            display_token = secrets.token_hex(32)
            screen = Screen(
                name=name,
                location=location,
                display_token=display_token,
                created_by=current_user.id
            )
            db.session.add(screen)
            db.session.commit()
            flash('Screen added successfully')
            return redirect(url_for('index'))
        
        flash('Screen name is required')
    
    return render_template('screen_form.html')

@app.route('/screen/<int:screen_id>/delete', methods=['POST'])
@login_required
def delete_screen(screen_id):
    if current_user.role != 'superuser':
        return jsonify({'error': 'Permission denied'}), 403

    screen = Screen.query.get_or_404(screen_id)
    db.session.delete(screen)
    db.session.commit()
    return jsonify({'message': 'Screen deleted successfully'})

@app.route('/screen/<int:screen_id>/media')
@login_required
def screen_media(screen_id):
    screen = Screen.query.get_or_404(screen_id)
    
    if current_user.role != 'superuser' and screen.created_by != current_user.id:
        flash('Permission denied')
        return redirect(url_for('index'))
    
    if current_user.role == 'superuser':
        media_items = Media.query.filter_by(screen_id=screen_id).all()
    else:
        media_items = Media.query.filter_by(
            screen_id=screen_id,
            uploaded_by=current_user.id
        ).all()
    
    scheduled_media = ScreenMedia.query.filter_by(screen_id=screen_id).all()
    return render_template('screen_media.html', screen=screen, media_items=media_items, scheduled_media=scheduled_media)

@app.route('/media/upload', methods=['POST'])
@login_required
def upload_media():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    
    screen_id = request.form.get('screen_id')
    if not screen_id:
        return jsonify({'error': 'Screen ID is required'}), 400
    
    screen = Screen.query.get_or_404(screen_id)
    if current_user.role != 'superuser' and screen.created_by != current_user.id:
        return jsonify({'error': 'Permission denied'}), 403
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        original_filename = filename
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_')
        filename = timestamp + filename
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(filepath)
        
        file_size = os.path.getsize(filepath)
        media_type = 'video' if filename.rsplit('.', 1)[1].lower() in ['mp4', 'mov'] else 'image'
        
        duration = None
        if media_type == 'video':
            video = VideoFileClip(filepath)
            duration = int(video.duration)
            video.close()
        
        media = Media(
            filename=filename,
            original_filename=original_filename,
            media_type=media_type,
            duration=duration,
            file_size=file_size,
            uploaded_by=current_user.id,
            screen_id=screen_id
        )
        
        db.session.add(media)
        db.session.commit()
        
        return jsonify({
            'message': 'File uploaded successfully',
            'media_id': media.id,
            'filename': filename
        })
    
    return jsonify({'error': 'File type not allowed'}), 400

@app.route('/media/<int:media_id>/delete', methods=['POST'])
@login_required
def delete_media(media_id):
    media = Media.query.get_or_404(media_id)
    
    if current_user.role != 'superuser' and media.uploaded_by != current_user.id:
        return jsonify({'error': 'Permission denied'}), 403
    
    try:
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], media.filename)
        if os.path.exists(filepath):
            os.remove(filepath)
        
        db.session.delete(media)
        db.session.commit()
        
        return jsonify({'message': 'Media deleted successfully'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/media/<int:media_id>/rotate', methods=['POST'])
@login_required
def rotate_media(media_id):
    media = Media.query.get_or_404(media_id)
    
    if current_user.role != 'superuser' and media.uploaded_by != current_user.id:
        return jsonify({'error': 'Permission denied'}), 403
    
    rotation = request.json.get('rotation', 0)
    
    if rotation not in [0, 90, 180, 270]:
        return jsonify({'error': 'Invalid rotation value'}), 400
    
    if media.media_type == 'image':
        filepath = os.path.join(app.config['UPLOAD_FOLDER'], media.filename)
        with Image.open(filepath) as img:
            rotated = img.rotate(-rotation, expand=True)
            rotated.save(filepath)
    
    media.rotation = rotation
    db.session.commit()
    
    return jsonify({'message': 'Media rotated successfully'})

@app.route('/screen/<int:screen_id>/schedule', methods=['POST'])
@login_required
def schedule_media(screen_id):
    screen = Screen.query.get_or_404(screen_id)
    
    if current_user.role != 'superuser' and screen.created_by != current_user.id:
        return jsonify({'error': 'Permission denied'}), 403
    
    media_id = request.json.get('media_id')
    media = Media.query.get_or_404(media_id)
    
    if media.screen_id != screen_id:
        return jsonify({'error': 'Media does not belong to this screen'}), 400
    
    start_time = datetime.fromisoformat(request.json.get('start_time'))
    end_time = datetime.fromisoformat(request.json.get('end_time'))
    
    if start_time >= end_time:
        return jsonify({'error': 'End time must be after start time'}), 400
    
    schedule = ScreenMedia(
        screen_id=screen_id,
        media_id=media_id,
        start_time=start_time,
        end_time=end_time
    )
    
    db.session.add(schedule)
    db.session.commit()
    
    return jsonify({'message': 'Media scheduled successfully'})

@app.route('/schedule/<int:schedule_id>/cancel', methods=['POST'])
@login_required
def cancel_schedule(schedule_id):
    schedule = ScreenMedia.query.get_or_404(schedule_id)
    
    if current_user.role != 'superuser' and schedule.screen.created_by != current_user.id:
        return jsonify({'error': 'Permission denied'}), 403
    
    schedule.status = 'cancelled'
    db.session.commit()
    
    return jsonify({'message': 'Schedule cancelled successfully'})

@app.route('/display/<token>')
def display_screen(token):
    screen = Screen.query.filter_by(display_token=token).first_or_404()
    current_time = datetime.utcnow()
    active_media = ScreenMedia.query.filter_by(
        screen_id=screen.id,
        status='scheduled'
    ).filter(
        ScreenMedia.start_time <= current_time,
        ScreenMedia.end_time >= current_time
    ).all()
    
    return render_template('display.html', screen=screen, active_media=active_media)

@app.route('/screen/<int:screen_id>/active-media')
def get_active_media(screen_id):
    screen = Screen.query.get_or_404(screen_id)
    current_time = datetime.utcnow()
    active_media = ScreenMedia.query.filter_by(
        screen_id=screen_id,
        status='scheduled'
    ).filter(
        ScreenMedia.start_time <= current_time,
        ScreenMedia.end_time >= current_time
    ).all()
    
    media_list = []
    for schedule in active_media:
        media_list.append({
            'id': schedule.media.id,
            'filename': schedule.media.filename,
            'media_type': schedule.media.media_type,
            'rotation': schedule.media.rotation,
            'duration': schedule.media.duration
        })
    
    return jsonify(media_list)

@app.route('/users')
@login_required
def users():
    if current_user.role != 'superuser':
        flash('Permission denied')
        return redirect(url_for('index'))
    
    users = User.query.all()
    return render_template('users.html', users=users)

@app.route('/user/add', methods=['GET', 'POST'])
@login_required
def add_user():
    if current_user.role != 'superuser':
        flash('Permission denied')
        return redirect(url_for('index'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        role = request.form.get('role')
        
        if username and password and role in ['admin', 'superuser']:
            if User.query.filter_by(username=username).first():
                flash('Username already exists')
            else:
                user = User(
                    username=username,
                    password_hash=generate_password_hash(password),
                    role=role
                )
                db.session.add(user)
                db.session.commit()
                flash('User added successfully')
                return redirect(url_for('users'))
        else:
            flash('All fields are required')
    
    return render_template('user_form.html')

def init_db():
    with app.app_context():
        db.create_all()
        if not User.query.filter_by(username='superuser').first():
            superuser = User(
                username='superuser',
                password_hash=generate_password_hash('superuser123'),
                role='superuser'
            )
            db.session.add(superuser)
            db.session.commit()

if __name__ == '__main__':
    if not os.path.exists('instance'):
        os.makedirs('instance')
    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        os.makedirs(app.config['UPLOAD_FOLDER'])
    init_db()
    app.run(host='0.0.0.0', port=5000, debug=True)