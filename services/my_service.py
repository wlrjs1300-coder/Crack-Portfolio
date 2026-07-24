from flask import Blueprint, current_app, render_template, session, redirect, url_for, request, jsonify
from werkzeug.security import check_password_hash, generate_password_hash
from extensions import db
from models import Member, Report, UserSettings, PointLog
from utils import check_profanity
from services.security import rate_limit
from services.location import validate_member_location

my_bp = Blueprint('my', __name__)


@my_bp.before_request
def require_mypage_login():
    if session.get('user_id'):
        return None
    if request.path.startswith('/api/') or request.is_json:
        return jsonify({'success': False, 'message': '로그인이 필요합니다.'}), 401
    return redirect(url_for('auth.login', next=request.full_path.rstrip('?')))


# [용어 정의] 상단바와 하단바를 제외한 실질적인 본문 영역을 '메인 콘텐츠 영역' 또는 '메인 영역'으로 정의합니다.
MAIN_CONTENT_AREA = "메인 콘텐츠 영역 (Main Content Area)"


@my_bp.route('/mypage')
def mypage():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('auth.login', next=request.full_path.rstrip('?')))

    member = db.session.get(Member, user_id)
    if not member:
        return redirect(url_for('auth.logout'))

    my_report_count = Report.query.filter_by(user_id=user_id).count()
    completed_count = Report.query.filter_by(user_id=user_id, status='처리완료').count()
    settings = UserSettings.query.filter_by(user_id=user_id).first()
    notification_enabled = settings.notification_enabled if settings else True
    point_logs = PointLog.query.filter_by(user_id=user_id).order_by(PointLog.created_at.desc()).all()

    return render_template('mypage.html',
                           my_report_count=my_report_count,
                           completed_count=completed_count,
                           notification_enabled=notification_enabled,
                           is_admin=session.get('is_admin', False),
                           member=member,
                           point_logs=point_logs
                           )


@my_bp.route('/mypage/profile')
def profile_page():
    member = db.session.get(Member, session['user_id'])
    if not member:
        session.clear()
        return redirect(url_for('auth.login'))
    return render_template('profile_edit.html', member=member)


@my_bp.route('/api/mypage/profile', methods=['POST'])
@rate_limit(10, 3600)
def update_profile():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'message': '로그인이 필요합니다.'}), 401

    data = request.get_json(silent=True) or {}
    member = db.session.get(Member, user_id)
    if not member:
        return jsonify({'success': False, 'message': '회원 정보를 찾을 수 없습니다.'}), 404

    if 'address' in data:
        location, location_error = validate_member_location(
            data.get('address'), data.get('latitude'), data.get('longitude'),
            data.get('region_city'), data.get('region_district')
        )
        if location_error:
            return jsonify({'success': False, 'message': location_error}), 400
        member.address = location['address']
        member.latitude = location['latitude']
        member.longitude = location['longitude']
        member.region_city = location['region_city']
        member.region_district = location['region_district']
        db.session.commit()
        return jsonify({'success': True, 'message': '내 주소가 저장되었습니다.'})

    # 닉네임 변경 로직
    if 'nickname' in data:
        if not isinstance(data['nickname'], str):
            return jsonify({'success': False, 'message': '닉네임 형식이 올바르지 않습니다.'}), 400
        new_nickname = data['nickname'].strip()
        if not new_nickname:
            return jsonify({'success': False, 'message': '닉네임을 입력해주세요.'}), 400
        if len(new_nickname) > 20:
            return jsonify({'success': False, 'message': '닉네임은 최대 20자까지 가능합니다.'}), 400
        if not check_profanity(new_nickname):
            return jsonify({'success': False, 'message': '부적절한 단어가 포함되어 있습니다.'}), 400
        duplicate = Member.query.filter(
            Member.nickname == new_nickname,
            Member.id != member.id,
        ).first()
        if duplicate:
            return jsonify({'success': False, 'message': '이미 사용 중인 닉네임입니다.'}), 409

        member.nickname = new_nickname
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            return jsonify({'success': False, 'message': '닉네임을 저장하지 못했습니다.'}), 500
        session['user_name'] = new_nickname  # 세션 닉네임 동기화
        return jsonify({'success': True, 'message': '닉네임이 변경되었습니다.'})

    # 비밀번호 변경 로직
    if 'current_password' in data and 'new_password' in data:
        curr_pw = data['current_password']
        new_pw = data['new_password']
        if not isinstance(curr_pw, str) or not isinstance(new_pw, str):
            return jsonify({'success': False, 'message': '비밀번호 형식이 올바르지 않습니다.'}), 400

        if len(new_pw) < 10 or len(new_pw) > 128:
            return jsonify({'success': False, 'message': '새 비밀번호는 10자 이상 128자 이하로 입력해주세요.'}), 400

        if not check_password_hash(member.password_hash, curr_pw):
            return jsonify({'success': False, 'message': '현재 비밀번호가 일치하지 않습니다.'}), 400

        member.password_hash = generate_password_hash(new_pw)
        db.session.commit()
        return jsonify({'success': True, 'message': '비밀번호가 변경되었습니다.'})

    # 관심지역 변경 로직
    if 'region_city' in data and 'region_district' in data:
        city = data['region_city']
        district = data['region_district']
        if not isinstance(city, str) or not isinstance(district, str) or len(city) > 50 or len(district) > 50:
            return jsonify({'success': False, 'message': '관심지역 형식이 올바르지 않습니다.'}), 400
        try:
            member.region_city = city
            member.region_district = district
            db.session.commit()
            return jsonify({'success': True, 'message': '관심지역이 저장되었습니다.'})
        except Exception as e:
            db.session.rollback()
            return jsonify({'success': False, 'message': '관심지역 저장 중 오류가 발생했습니다.'}), 500

    return jsonify({'success': False, 'message': '잘못된 요청입니다.'}), 400


@my_bp.route('/api/mypage/settings', methods=['POST'])
def update_settings():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'message': '로그인이 필요합니다.'}), 401

    data = request.get_json(silent=True) or {}
    enabled = data.get('notification_enabled', True)
    if not isinstance(enabled, bool):
        return jsonify({'success': False, 'message': '알림 설정 형식이 올바르지 않습니다.'}), 400

    settings = UserSettings.query.filter_by(user_id=user_id).first()
    if not settings:
        settings = UserSettings(user_id=user_id, notification_enabled=enabled)
        db.session.add(settings)
    else:
        settings.notification_enabled = enabled

    db.session.commit()
    return jsonify({'success': True})


@my_bp.route('/api/withdraw', methods=['POST'])
@rate_limit(3, 3600)
def withdraw():
    """회원 탈퇴 API"""
    if not session.get('user_id'):
        return jsonify({'success': False, 'message': '로그인이 필요합니다.'}), 401

    data = request.get_json(silent=True) or {}
    password_confirm = data.get('password_confirm', '')

    user = db.session.get(Member, session['user_id'])

    if not check_password_hash(user.password_hash, password_confirm):
        return jsonify({'success': False, 'message': '비밀번호가 일치하지 않습니다.'}), 400

    try:
        from services.retention import _delete_upload
        for report in Report.query.filter_by(user_id=user.id).all():
            _delete_upload(current_app._get_current_object(), report.file_path)
            if report.thumbnail_path != report.file_path:
                _delete_upload(current_app._get_current_object(), report.thumbnail_path)
        db.session.delete(user)
        db.session.commit()
        session.clear()
        return jsonify({'success': True, 'message': '그동안 이용해주셔서 감사합니다.'})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': '탈퇴 처리중 오류가 발생했습니다.'}), 500
