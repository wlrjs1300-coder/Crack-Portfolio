from flask import Blueprint, render_template, session, redirect, url_for, request, jsonify
from extensions import db, socketio
from models import Report, CrackTalk, Member
from utils import check_profanity
from flask import current_app
from flask_socketio import join_room
from services.media_security import MediaValidationError, sanitize_image_to_jpeg, save_and_validate
from services.security import rate_limit
from services.privacy_filter import PrivacyFilterError, blur_image_in_place, blur_video_in_place
from sqlalchemy import update

status_bp = Blueprint('status', __name__)

# [용어 정의] 상단바와 하단바를 제외한 실질적인 본문 영역을 '메인 콘텐츠 영역' 또는 '메인 영역'으로 정의합니다.
MAIN_CONTENT_AREA = "메인 콘텐츠 영역 (Main Content Area)"
import os


@socketio.on('connect')
def join_authenticated_socket_room(auth=None):
    """채팅 이벤트는 로그인된 소켓에만 전달한다."""
    if session.get('user_id'):
        join_room('authenticated')


def _normalize_path(path):
    if not path:
        return ''
    path = path.replace('\\', '/')
    if path.startswith('http') or path.startswith('data:'):
        return path
    if not path.startswith('/'):
        if path.startswith('uploads/'):
            path = '/' + path
        else:
            path = '/uploads/' + path
    return path


@status_bp.route('/status')
def status():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('auth.login'))

    db_reports = Report.query.filter(
        Report.user_id == user_id,
        Report.status != '삭제'
    ).order_by(Report.created_at.desc()).all()

    my_reports = []
    for r in db_reports:
        # 확장자 기반 file_type 판별 보강
        ext_video = (r.file_path or '').lower().endswith(('.mp4', '.mov', '.avi', '.m4v'))
        f_type = 'video' if ext_video else (r.file_type or 'image')

        my_reports.append({
            'id': r.id,
            'title': r.title or '제목 없음',
            'status': r.status,
            'date': r.created_at.strftime('%Y-%m-%d') if r.created_at else '',
            'file_path': _normalize_path(r.file_path),
            'thumbnail_path': _normalize_path(r.thumbnail_path),
            'file_type': f_type,
            'reject_reason': r.reject_reason
        })
    return render_template('status.html', reports=my_reports)


@status_bp.route('/api/cracktalk', methods=['GET'])
def get_cracktalk():
    if not session.get('user_id'):
        return jsonify({'success': False, 'message': '로그인이 필요합니다.'}), 401
    is_admin = session.get('is_admin', False)
    # 최근 50개 메시지를 가져온 후, 다시 시간순으로 정렬
    talks = CrackTalk.query.order_by(CrackTalk.created_at.desc()).limit(50).all()
    talks.reverse()  # 올바른 순서를 위해 목록을 뒤집음
    result = []
    for t in talks:
        if t.is_blinded and not is_admin:
            # 일반 회원: 블라인드 처리된 메시지는 내용 숨김
            result.append({
                'id': t.id,
                'author_id': None,
                'nickname': '',
                'content': '',
                'date': t.created_at.strftime('%m-%d %H:%M'),
                'is_blinded': True
            })
        else:
            # 관리자 또는 정상 메시지: 전체 노출
            result.append({
                'id': t.id,
                'author_id': t.author_id,
                'nickname': t.author.nickname if t.author else '익명',
                'content': t.content,
                'date': t.created_at.strftime('%m-%d %H:%M'),
                'is_blinded': t.is_blinded
            })
    return jsonify(result)


@status_bp.route('/api/cracktalk', methods=['POST'])
@rate_limit(20, 60)
def post_cracktalk():
    from models import PointLog  # 순환 참조 방지를 위해 여기서 import
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'message': '로그인이 필요합니다.'}), 401

    data = request.get_json(silent=True) or {}
    content = data.get('content', '').strip()

    if not content:
        return jsonify({'success': False, 'message': '내용을 입력해주세요.'}), 400
    if len(content) > 1000:
        return jsonify({'success': False, 'message': '메시지는 최대 1000자까지 입력할 수 있습니다.'}), 400

    # 비속어 필터링 적용
    if not check_profanity(content):
        return jsonify({'success': False, 'message': '부적절한 단어가 포함되어 있습니다. 바른 말을 사용해 주세요.'}), 400

    user = db.session.get(Member, user_id)
    # 일반 사용자일 경우 크래커 포인트 20점 차감 (관리자는 무제한)
    if not user.is_admin:
        debit = db.session.execute(
            update(Member).where(Member.id == user_id, Member.points >= 20).values(
                points=Member.points - 20
            )
        )
        if debit.rowcount != 1:
            db.session.rollback()
            return jsonify({'success': False, 'message': '보유한 크래커가 부족합니다. (20 크래커 필요)'}), 400
        db.session.add(PointLog(user_id=user_id, amount=-20, reason='크랙톡 채팅 작성 (포인트 소모)'))
    else:
        # 관리자도 내역 확인을 위해 0점 로그 추가
        db.session.add(PointLog(user_id=user_id, amount=0, reason='크랙톡 채팅 작성 (관리자 무료)'))

    new_talk = CrackTalk(author_id=user_id, content=content)
    db.session.add(new_talk)
    try:
        db.session.commit()
        # [WEB-SOCKET] 실시간 CrackTalk 브로드캐스트
        session_user = db.session.get(Member, user_id)
        socketio.emit('new_message', {
            'id': new_talk.id,
            'author_id': new_talk.author_id,
            'nickname': session_user.nickname if session_user else '익명',
            'content': new_talk.content,
            'date': new_talk.created_at.strftime('%m-%d %H:%M'),
            'is_blinded': False
        }, namespace='/', to='authenticated')
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': '저장 중 오류가 발생했습니다.'}), 500

    return jsonify({'success': True})


# 기존 DELETE 삭제 → PATCH 블라인드 토글로 교체
@status_bp.route('/api/cracktalk/blind/<int:talk_id>', methods=['PATCH'])
def toggle_blind_cracktalk(talk_id):
    if not session.get('is_admin'):
        return jsonify({'success': False, 'message': '권한이 없습니다.'}), 403

    talk = db.get_or_404(CrackTalk, talk_id)
    try:
        talk.is_blinded = not talk.is_blinded  # 블라인드 ↔ 노출 토글
        db.session.commit()
        return jsonify({'success': True, 'is_blinded': talk.is_blinded})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'message': '처리 중 오류가 발생했습니다.'}), 500


@status_bp.route('/api/report/<int:report_id>/update', methods=['POST'])
@rate_limit(10, 3600)
def update_report(report_id):
    from sqlalchemy import text as sa_text

    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'message': '로그인이 필요합니다.'}), 401

    report = db.get_or_404(Report, report_id)

    # DB에서 직접 admin 여부 확인
    row = db.session.execute(
        sa_text("SELECT is_admin, role FROM members WHERE id = :uid LIMIT 1"),
        {'uid': user_id}
    ).mappings().first()

    is_admin = False
    if row:
        is_admin = (row.get('is_admin') == 1) or (row.get('role') == 'admin')

    # 관리자는 모든 글 수정 가능, 일반 사용자는 본인 글만
    if not is_admin and str(report.user_id) != str(user_id):
        return jsonify({'success': False, 'message': '권한이 없습니다.'}), 403

    title = (request.form.get('title') or '').strip()
    content = (request.form.get('content') or '').strip()
    if not title or len(title) > 30 or len(content) > 5000:
        return jsonify({'success': False, 'message': '제목 또는 내용이 허용 길이를 벗어났습니다.'}), 400
    report.title = title
    report.content = content

    file = request.files.get('file')
    if file and file.filename != '':
        try:
            save_path, uploaded_path, media_kind = save_and_validate(file)
        except MediaValidationError as exc:
            return jsonify({'success': False, 'message': str(exc)}), 400

        if media_kind == 'video':
            from services.report_service import convert_to_mp4
            save_path, uploaded_path = convert_to_mp4(save_path, '', os.path.basename(save_path))
            try:
                save_path = blur_video_in_place(save_path)
                uploaded_path = f'/uploads/videos/{os.path.basename(save_path)}'
            except PrivacyFilterError as exc:
                os.remove(save_path)
                return jsonify({'success': False, 'message': str(exc)}), 500
            report.file_path = uploaded_path
            report.file_type = 'video'
            report.thumbnail_path = None
        else:
            try:
                blur_image_in_place(save_path)
                _, uploaded_path = sanitize_image_to_jpeg(save_path)
            except (MediaValidationError, PrivacyFilterError) as exc:
                if os.path.isfile(save_path):
                    os.remove(save_path)
                return jsonify({'success': False, 'message': str(exc)}), 400
            report.file_path = uploaded_path
            report.file_type = 'image'
            report.thumbnail_path = uploaded_path

    try:
        db.session.commit()
        try:
            file_path = report.file_path
            file_type = report.file_type or (
                'video' if (file_path or '').lower().endswith(('.mp4', '.mov', '.avi', '.m4v')) else 'image')
            report.status = 'AI 분석중'
            db.session.commit()
            if not current_app.config.get('AI_AVAILABLE', False) or not current_app.submit_ai_analysis(report.id, file_path, file_type):
                report.status = '관리자 확인중'
                report.reject_reason = 'AI 분석 대기열이 가득 차 관리자 수동 확인으로 전환되었습니다.'
                db.session.commit()
        except Exception as ai_err:
            current_app.logger.exception('AI 재분석 작업 등록 실패')
            report.status = '관리자 확인중'
            report.reject_reason = 'AI 분석 작업을 등록하지 못해 관리자 수동 확인으로 전환되었습니다.'
            db.session.commit()

        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception('신고 수정 실패')
        return jsonify({'success': False, 'message': '신고 수정 중 오류가 발생했습니다.'}), 500


# [NEW] 소프트 삭제 - DB에서 실제 삭제하지 않고 status만 '삭제'로 변경
@status_bp.route('/api/report/<int:report_id>/soft-delete', methods=['POST'])
def soft_delete_report(report_id):
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'message': '로그인이 필요합니다.'}), 401

    report = db.get_or_404(Report, report_id)

    # 본인 확인
    if str(report.user_id) != str(user_id):
        return jsonify({'success': False, 'message': '권한이 없습니다.'}), 403

    try:
        report.status = '삭제'
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception('신고 소프트 삭제 실패')
        return jsonify({'success': False, 'message': '삭제 처리 중 오류가 발생했습니다.'}), 500


@status_bp.route('/api/report/<int:report_id>/delete', methods=['POST'])
def delete_my_report(report_id):
    from sqlalchemy import text as sa_text

    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'message': '로그인이 필요합니다.'}), 401

    report = db.session.get(Report, report_id)
    if not report:
        return jsonify({'success': False, 'message': '존재하지 않는 게시글입니다.'}), 404

    # ✅ DB에서 직접 admin 여부 확인
    row = db.session.execute(
        sa_text("SELECT is_admin, role FROM members WHERE id = :uid LIMIT 1"),
        {'uid': user_id}
    ).mappings().first()

    is_admin = False
    if row:
        is_admin = (row.get('is_admin') == 1) or (row.get('role') == 'admin')

    # 관리자는 모든 글 삭제 가능, 일반 사용자는 본인 글만
    if not is_admin and str(report.user_id) != str(user_id):
        return jsonify({'success': False, 'message': '삭제 권한이 없습니다.'}), 403

    try:
        report.status = '삭제'
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        current_app.logger.exception('신고 삭제 실패')
        return jsonify({'success': False, 'message': '삭제 처리 중 오류가 발생했습니다.'}), 500
