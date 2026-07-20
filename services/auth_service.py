import os
import json
import re
import hashlib
import secrets
import smtplib
from datetime import timedelta
from email.message import EmailMessage
from urllib.parse import urlencode
from flask import Blueprint, render_template, session, redirect, url_for, request, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from extensions import db
from models import Member, PasswordResetToken
from sqlalchemy.exc import IntegrityError
from utils import check_profanity, get_now_kst
from services.security import rate_limit

auth_bp = Blueprint('auth', __name__)

# [용어 정의] 상단바와 하단바를 제외한 실질적인 본문 영역을 '메인 콘텐츠 영역' 또는 '메인 영역'으로 정의합니다.
MAIN_CONTENT_AREA = "메인 콘텐츠 영역 (Main Content Area)"


@auth_bp.route('/login', methods=['GET', 'POST'])
@rate_limit(10, 300)
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        user = Member.query.filter_by(username=username).first()
        if user and not user.active:
            return render_template('login.html', error="사용이 정지된 계정입니다. 관리자에게 문의해주세요."), 403
        if user and check_password_hash(user.password_hash, password):
            session.clear()
            session['user_id'] = user.id
            session['user_name'] = user.nickname if user.nickname else user.username
            role = 'admin' if user.is_admin else (
                user.role if user.role in ('manager', 'user') else 'user'
            )
            session['is_admin'] = role == 'admin'
            session['user_role'] = role
            session['role'] = role
            return redirect(url_for('index'))
        else:
            return render_template('login.html', error="아이디 또는 비밀번호가 잘못되었습니다.")

    return render_template('login.html')


@auth_bp.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('index'))


# ... 기존 import 생략

@auth_bp.route('/signup', methods=['GET', 'POST'])
@rate_limit(5, 3600)
def signup():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        nickname = request.form.get('nickname')
        email = (request.form.get('email') or '').strip().lower()

        if not username or not re.fullmatch(r'[A-Za-z0-9_]{4,30}', username):
            return render_template('signup.html', error="아이디는 영문, 숫자, 밑줄 조합 4~30자로 입력해주세요."), 400
        if not password or len(password) < 10 or len(password) > 128:
            return render_template('signup.html', error="비밀번호는 10자 이상 128자 이하로 입력해주세요."), 400

        # 1. 이메일 형식 및 @ 앞부분 영문/숫자 체크
        # 규칙: 시작은 영문/숫자, @ 앞까지 영문/숫자만 허용
        email_pattern = r'^[a-zA-Z0-9]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'

        if not email or not re.match(email_pattern, email):
            return render_template('signup.html', error="이메일 형식이 올바르지 않거나, 아이디 부분에 특수문자를 사용할 수 없습니다.")
        # 기본 검증
        if not nickname or len(nickname) > 20:
            return render_template('signup.html', error="닉네임은 1자 이상 20자 이하로 입력해주세요.")

        if not check_profanity(nickname):
            return render_template('signup.html', error="닉네임에 부적절한 단어가 포함되어 있습니다.")

        # 중복 검사 (아이디, 닉네임, 이메일)
        if Member.query.filter_by(username=username).first():
            return render_template('signup.html', error="이미 존재하는 아이디입니다.")

        if Member.query.filter_by(nickname=nickname).first():
            return render_template('signup.html', error="이미 존재하는 닉네임입니다. 다른 닉네임을 사용해주세요.")

        if Member.query.filter_by(email=email).first():  # 이메일 중복 체크 추가
            return render_template('signup.html', error="이미 등록된 이메일입니다.")

        hashed_pw = generate_password_hash(password)
        # DB 모델에 email 필드가 있다고 가정 (new_user 생성 시 추가)
        new_user = Member(username=username, password_hash=hashed_pw, nickname=nickname, email=email, points=0)
        try:
            db.session.add(new_user)
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return render_template('signup.html', error="이미 사용 중인 회원 정보입니다."), 409

        return redirect(url_for('auth.login'))

    return render_template('signup.html')


# 이메일 중복 확인 API 추가
@auth_bp.route('/api/check_email', methods=['POST'])
@rate_limit(20, 60)
def check_email():
    data = request.get_json(silent=True) or {}
    email = data.get('email')
    if not isinstance(email, str) or len(email) > 120:
        return jsonify({'available': False, 'message': '이메일 형식이 올바르지 않습니다.'}), 400
    email = email.strip().lower()

    # 정규식 패턴 (기존과 동일하게 유지)
    email_pattern = r'^[a-zA-Z0-9]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$'

    if not email or not re.match(email_pattern, email):
        return jsonify({'available': False, 'message': '영문/숫자 조합의 올바른 이메일 형식을 입력해주세요.'}), 400

    user = Member.query.filter_by(email=email).first()
    if user:
        return jsonify({'available': False, 'message': '이미 사용 중인 이메일입니다.'})
    else:
        return jsonify({'available': True, 'message': '사용 가능한 이메일입니다.'})


@auth_bp.route('/api/check_id', methods=['POST'])
@rate_limit(20, 60)
def check_id():
    data = request.get_json(silent=True) or {}
    username = data.get('username')

    if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9_]{4,30}', username):
        return jsonify({'available': False, 'message': '아이디를 입력해주세요.'}), 400

    user = Member.query.filter_by(username=username).first()
    if user:
        return jsonify({'available': False, 'message': '이미 존재하는 아이디입니다.'})
    else:
        return jsonify({'available': True, 'message': '사용 가능한 아이디입니다.'})


@auth_bp.route('/api/find-id', methods=['POST'])
@rate_limit(5, 900)
def find_id():
    data = request.get_json(silent=True) or {}
    nickname = data.get('name')
    email = data.get('email')
    if not isinstance(nickname, str) or not isinstance(email, str):
        return jsonify({'success': False, 'message': "일치하는 회원 정보가 없습니다."})
    nickname = nickname.strip()
    email = email.strip().lower()
    if len(nickname) > 80 or len(email) > 120:
        return jsonify({'success': False, 'message': "일치하는 회원 정보가 없습니다."})

    # DB에서 이름(nickname)과 이메일이 모두 일치하는 사용자 검색
    user = Member.query.filter_by(nickname=nickname, email=email).first()

    if user:
        return jsonify({
            'success': True,
            'username': user.username,
            'message': f"찾으시는 아이디는 '{user.username}' 입니다."
        })
    else:
        return jsonify({
            'success': False,
            'message': "일치하는 회원 정보가 없습니다."
        })


@auth_bp.route('/api/find-pw', methods=['POST'])
@rate_limit(5, 900)
def find_password():
    data = request.get_json(silent=True) or {}
    generic_message = '입력한 정보가 등록되어 있다면 비밀번호 재설정 메일을 발송했습니다.'
    username_value = data.get('username')
    email_value = data.get('email')
    if not isinstance(username_value, str) or not isinstance(email_value, str):
        return jsonify({'success': True, 'message': generic_message})
    username = username_value.strip()
    email = email_value.strip().lower()

    smtp_host = os.getenv('SMTP_HOST')
    smtp_from = os.getenv('SMTP_FROM')
    if not smtp_host or not smtp_from:
        return jsonify({'success': False, 'message': '현재 이메일 발송 서비스를 사용할 수 없습니다.'}), 503

    if len(username) > 30 or len(email) > 120:
        return jsonify({'success': True, 'message': generic_message})
    user = Member.query.filter_by(username=username, email=email, active=True).first()
    if not user:
        return jsonify({'success': True, 'message': generic_message})

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode('utf-8')).hexdigest()
    expires_at = get_now_kst() + timedelta(minutes=20)
    reset_token = PasswordResetToken(user_id=user.id, token_hash=token_hash, expires_at=expires_at)
    db.session.add(reset_token)

    base_url = (os.getenv('PUBLIC_BASE_URL') or request.url_root).rstrip('/')
    reset_url = f"{base_url}/login?{urlencode({'reset_token': raw_token, 'username': user.username})}"
    message = EmailMessage()
    message['Subject'] = '[CRACK] 비밀번호 재설정 안내'
    message['From'] = smtp_from
    message['To'] = user.email
    message.set_content(
        '아래 링크는 20분 동안 한 번만 사용할 수 있습니다.\n\n'
        f'{reset_url}\n\n본인이 요청하지 않았다면 이 메일을 무시해주세요.',
        charset='utf-8',
    )

    try:
        port = int(os.getenv('SMTP_PORT', '587'))
        use_ssl = os.getenv('SMTP_USE_SSL', '').lower() in ('1', 'true', 'yes')
        smtp_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with smtp_cls(smtp_host, port, timeout=10) as smtp:
            starttls = os.getenv('SMTP_STARTTLS', os.getenv('SMTP_USE_TLS', 'true'))
            if not use_ssl and starttls.lower() in ('1', 'true', 'yes'):
                smtp.starttls()
            smtp_user = os.getenv('SMTP_USER') or os.getenv('SMTP_USERNAME')
            smtp_password = os.getenv('SMTP_PASSWORD')
            if smtp_user and smtp_password:
                smtp.login(smtp_user, smtp_password)
            smtp.send_message(message)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'message': '재설정 메일 발송에 실패했습니다. 잠시 후 다시 시도해주세요.'}), 503

    return jsonify({'success': True, 'message': generic_message})


@auth_bp.route('/api/reset-pw', methods=['POST'])
@rate_limit(5, 900)
def reset_pw():
    data = request.get_json(silent=True) or {}
    username_value = data.get('username')
    if not isinstance(username_value, str):
        return jsonify({'success': False, 'message': '재설정 요청 형식이 올바르지 않습니다.'}), 400
    username = username_value.strip()
    new_password = data.get('password') or ''
    raw_token = data.get('token') or ''
    if not isinstance(new_password, str) or not isinstance(raw_token, str):
        return jsonify({'success': False, 'message': '재설정 요청 형식이 올바르지 않습니다.'}), 400
    if len(username) > 30 or len(raw_token) > 200:
        return jsonify({'success': False, 'message': '재설정 링크가 유효하지 않거나 만료되었습니다.'}), 400
    if len(new_password) < 10 or len(new_password) > 128:
        return jsonify({'success': False, 'message': '비밀번호는 10자 이상 128자 이하로 입력해주세요.'}), 400
    if not raw_token:
        return jsonify({'success': False, 'message': '재설정 인증 정보가 없습니다.'}), 400

    user = Member.query.filter_by(username=username, active=True).first()
    token_hash = hashlib.sha256(raw_token.encode('utf-8')).hexdigest()
    token_row = PasswordResetToken.query.filter_by(token_hash=token_hash, used_at=None).first()
    now = get_now_kst()
    if not user or not token_row or token_row.user_id != user.id or token_row.expires_at < now:
        return jsonify({'success': False, 'message': '재설정 링크가 유효하지 않거나 만료되었습니다.'}), 400

    try:
        claimed = PasswordResetToken.query.filter(
            PasswordResetToken.id == token_row.id,
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at >= now,
        ).update({'used_at': now}, synchronize_session=False)
        if claimed != 1:
            db.session.rollback()
            return jsonify({'success': False, 'message': '재설정 링크가 이미 사용되었거나 만료되었습니다.'}), 400
        user.password_hash = generate_password_hash(new_password)
        PasswordResetToken.query.filter(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.id != token_row.id,
            PasswordResetToken.used_at.is_(None),
        ).update({'used_at': now}, synchronize_session=False)
        db.session.commit()
        session.clear()
        return jsonify({'success': True, 'message': '비밀번호가 변경되었습니다.'})
    except Exception:
        db.session.rollback()
        return jsonify({'success': False, 'message': '비밀번호 변경 중 오류가 발생했습니다.'}), 500
