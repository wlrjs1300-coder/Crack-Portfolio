import math
from collections import defaultdict
from datetime import datetime, timedelta

from services.region_service import normalize_region_name, parse_region_hierarchy
from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, current_app
from sqlalchemy import text

from extensions import db, socketio

admin_bp = Blueprint('admin', __name__)


def _safe_float(value, default=0.0):
    try:
        if value is None or value == '':
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value, default=0):
    try:
        if value is None or value == '':
            return default
        return int(value)
    except Exception:
        return default


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


def _parse_dt(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d'):
            try:
                return datetime.strptime(value, fmt)
            except Exception:
                pass
    return None


def haversine_m(lat1, lon1, lat2, lon2):
    lat1 = _safe_float(lat1)
    lon1 = _safe_float(lon1)
    lat2 = _safe_float(lat2)
    lon2 = _safe_float(lon2)
    if not (lat1 or lon1 or lat2 or lon2):
        return 999999.0
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlon / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _current_user_role():
    role = session.get('user_role') or session.get('role')
    if role:
        return role
    user_id = session.get('user_id')
    if not user_id:
        return 'user'
    row = db.session.execute(text("""
        SELECT
            COALESCE(role, CASE WHEN is_admin = 1 THEN 'admin' ELSE 'user' END) AS role_value,
            is_admin,
            nickname,
            username
        FROM members
        WHERE id = :user_id
        LIMIT 1
    """), {'user_id': user_id}).mappings().first()
    if not row:
        return 'user'
    role_value = row.get('role_value') or ('admin' if _safe_int(row.get('is_admin')) == 1 else 'user')
    session['user_role'] = role_value
    session['role'] = role_value
    session['is_admin'] = role_value == 'admin' or _safe_int(row.get('is_admin')) == 1
    session['user_name'] = row.get('nickname') or row.get('username') or '관리자'
    return role_value


def _require_admin():
    if not session.get('user_id'):
        return redirect(url_for('auth.login'))
    if _current_user_role() != 'admin':
        return redirect(url_for('index'))
    return None


def _latest_ai_join_sql():
    return """
        LEFT JOIN (
            SELECT a1.*
            FROM ai_results a1
            INNER JOIN (
                SELECT report_id, MAX(id) AS max_id
                FROM ai_results
                GROUP BY report_id
            ) a2 ON a1.id = a2.max_id
        ) ai ON ai.report_id = r.id
    """


def _fetch_reports():
    sql = text(f"""
        SELECT
            r.id,
            r.title,
            r.content,
            r.latitude,
            r.longitude,
            r.file_path,
            r.file_type,
            r.created_at,
            r.user_id,
            r.address,
            r.status,
            r.reject_reason,
            r.region_name,
            r.last_checked_at,
            r.thumbnail_path,
            ai.is_damaged,
            ai.confidence,
            ai.damage_type,
            m.username,
            m.nickname,
            m.manager_region,
            COALESCE(m.role, CASE WHEN m.is_admin = 1 THEN 'admin' ELSE 'user' END) AS member_role,
            m.is_admin,
            m.active
        FROM report r
        {_latest_ai_join_sql()}
        LEFT JOIN members m ON m.id = r.user_id
        ORDER BY r.created_at DESC, r.id DESC
    """)
    rows = []
    for row in db.session.execute(sql).mappings().all():
        item = dict(row)
        item['created_at'] = _parse_dt(item.get('created_at'))
        item['risk_score'] = _safe_float(item.get('confidence'))
        # 경로 정규화 및 형식 판별 적용
        item['file_path'] = _normalize_path(item.get('file_path'))
        item['thumbnail_path'] = _normalize_path(item.get('thumbnail_path'))
        item['image_path'] = item['thumbnail_path'] or item['file_path'] or ''
        # 동영상 확장자 체크 추가
        if (item.get('file_path') or '').lower().endswith(('.mp4', '.mov', '.avi', '.m4v')):
            item['file_type'] = 'video'

        item['location'] = item.get('region_name') or item.get('address') or '위치 정보 없음'
        item['first_created_at'] = item['created_at']
        rows.append(item)
    return rows


def _build_groups(items):
    groups = []
    visited = set()
    for item in items:
        if item['id'] in visited:
            continue
        component = []
        queue = [item]
        visited.add(item['id'])
        while queue:
            current = queue.pop()
            component.append(current)
            current_dt = current.get('created_at')
            for other in items:
                if other['id'] in visited:
                    continue
                other_dt = other.get('created_at')
                if current_dt is None or other_dt is None:
                    continue
                if abs((current_dt - other_dt).total_seconds()) > 86400:
                    continue
                if haversine_m(current.get('latitude'), current.get('longitude'), other.get('latitude'), other.get('longitude')) > 50:
                    continue
                visited.add(other['id'])
                queue.append(other)
        groups.append(component)
    group_map = {}
    for group in groups:
        distinct_users = len({g.get('user_id') for g in group if g.get('user_id') is not None}) or 1
        representative = max(
            group,
            key=lambda x: (
                x.get('created_at') or datetime.min,
                x.get('id') or 0
            )
        )
        target_status = representative.get('status') or ''
        target_reject_reason = representative.get('reject_reason') or ''
        for member in group:
            status = member.get('status') or ''
            created_at = member.get('created_at')
            urgent_reasons = []
            repeat_count = max(0, distinct_users - 1)
            if _safe_float(member.get('risk_score')) >= 80:
                urgent_reasons.append('고위험')
            if repeat_count >= 2:
                urgent_reasons.append('반복 제보')
            if created_at and status in ('접수완료', '관리자 확인중') and (datetime.now() - created_at).total_seconds() >= 86400:
                urgent_reasons.append('처리 지연')
            member['group_reporter_count'] = repeat_count
            member['urgent_reason'] = ', '.join(urgent_reasons)
            member['priority_score'] = _priority_score(member)
            group_map[member['id']] = {
                'group_ids': [g['id'] for g in group],
                'representative_id': representative.get('id'),
                'group_reporter_count': repeat_count,
                'urgent_reason': member['urgent_reason'],
                'status': target_status,
                'reject_reason': target_reject_reason,
            }
    return group_map


def _priority_score(item):
    score = 0
    status = item.get('status') or ''
    risk_score = _safe_float(item.get('risk_score'))
    repeat_count = _safe_int(item.get('group_reporter_count'), 0)
    created_at = item.get('created_at')

    if status in ('접수완료', '관리자 확인중'):
        score += 100

    if risk_score >= 80:
        score += 50
    elif risk_score >= 50:
        score += 20

    if repeat_count >= 4:
        score += 40
    elif repeat_count >= 2:
        score += 30
    elif repeat_count >= 1:
        score += 10

    if created_at and status in ('접수완료', '관리자 확인중') and (datetime.now() - created_at).total_seconds() >= 86400:
        score += 40

    return score


def _status_rank(status):
    order = {
        '관리자 확인중': 0,
        '접수완료': 1,
        '처리중': 2,
        '처리중': 3,
        '처리완료': 4,
        '반려': 5,
        '삭제': 6,
    }
    return order.get((status or '').strip(), 99)


def _hydrate_reports():
    reports = _fetch_reports()
    group_map = _build_groups(reports)

    for item in reports:
        meta = group_map.get(item['id'], {})
        item['group_reporter_count'] = meta.get('group_reporter_count', 0)
        item['urgent_reason'] = meta.get('urgent_reason', '')
        item['priority_score'] = _priority_score(item)
        item['group_ids'] = meta.get('group_ids', [item['id']])
        item['representative_id'] = meta.get('representative_id', item['id'])

    representative_reports = [
        item for item in reports
        if _safe_int(item.get('id')) == _safe_int(item.get('representative_id'))
    ]

    return reports, representative_reports, group_map


def _member_name(row):
    return row.get('nickname') or row.get('username') or f"회원 {row.get('id')}"


def _member_uid(row):
    return row.get('username') or '-'


@admin_bp.route('/admin/dashboard')
def admin_dashboard():
    denied = _require_admin()
    if denied:
        return denied

    selected_tab = request.args.get('tab', 'urgent').strip() or 'pending'
    page = max(_safe_int(request.args.get('page', 1), 1), 1)
    anchor_index = _safe_int(request.args.get('anchor_index'), None)
    per_page = _safe_int(request.args.get('page_size', 8), 8)

    if per_page < 4:
        per_page = 4
    elif per_page > 12:
        per_page = 12

    reports, representative_reports, _ = _hydrate_reports()
    now = datetime.now()
    today = now.date()

    dashboard_items = []

    def is_pending(item):
        return (item.get('status') or '') in ('관리자 확인중', '접수완료')

    def is_long_pending(item):
        created_at = item.get('created_at')
        return is_pending(item) and created_at and (now - created_at).total_seconds() >= 86400

    def is_urgent(item):
        return is_pending(item) and (
                _safe_float(item.get('risk_score')) >= 80
                or _safe_int(item.get('group_reporter_count'), 0) >= 2
                or is_long_pending(item)
        )

    summary = {
        'urgent_count': sum(1 for item in reports if is_urgent(item)),
        'today_count': sum(1 for item in reports if item.get('created_at') and item['created_at'].date() == today),
        'pending_count': sum(1 for item in reports if is_pending(item)),
        'processing_count': sum(1 for item in reports if (item.get('status') or '') == '처리중'),
        'rejected_count': sum(1 for item in reports if (item.get('status') or '') == '반려'),
    }

    if selected_tab == 'urgent':
        dashboard_items = [item for item in reports if is_urgent(item)]
        dashboard_section_title = '긴급 신고'
        dashboard_section_subtitle = '우선 검토가 필요한 신고입니다.'
    elif selected_tab == 'today':
        dashboard_items = [item for item in reports if item.get('created_at') and item['created_at'].date() == today]
        dashboard_section_title = '오늘 접수'
        dashboard_section_subtitle = '오늘 들어온 신고 목록입니다.'
    elif selected_tab == 'long_pending':
        dashboard_items = [item for item in reports if (item.get('status') or '') == '처리중']
        dashboard_section_title = '처리중'
        dashboard_section_subtitle = '현재 처리중인 신고 목록입니다.'
    elif selected_tab == 'rejected':
        dashboard_items = [item for item in reports if (item.get('status') or '') == '반려']
        dashboard_section_title = '반려 신고'
        dashboard_section_subtitle = '반려 처리된 신고 목록입니다.'
    else:
        selected_tab = 'pending'
        dashboard_items = [item for item in reports if is_pending(item)]
        dashboard_section_title = '미처리 신고'
        dashboard_section_subtitle = '현재 검토가 필요한 신고 목록입니다.'

    dashboard_items.sort(
        key=lambda x: (_priority_score(x), _safe_float(x.get('risk_score')), x.get('created_at') or datetime.min),
        reverse=True
    )

    total_count = len(dashboard_items)
    total_pages = max(1, math.ceil(total_count / per_page))

    if anchor_index is not None and anchor_index >= 0:
        page = (anchor_index // per_page) + 1

    if page > total_pages:
        page = total_pages

    start = (page - 1) * per_page
    end = start + per_page
    dashboard_items = dashboard_items[start:end]

    return render_template(
        'admin_dashboard.html',
        selected_tab=selected_tab,
        summary=summary,
        dashboard_items=dashboard_items,
        dashboard_section_title=dashboard_section_title,
        dashboard_section_subtitle=dashboard_section_subtitle,
        current_page=page,
        total_pages=total_pages,
        total_count=total_count,
        KAKAO_JS_KEY=current_app.config.get('KAKAO_JS_KEY', ''),
    )


@admin_bp.route('/admin/incidents')
def admin_incidents():
    member_id = request.args.get('member_id', type=int)
    denied = _require_admin()
    if denied:
        return denied

    quick_filter = request.args.get('quick_filter', '').strip()
    selected_status = request.args.get('status', '').strip()
    selected_risk = request.args.get('risk', '').strip()
    selected_region = request.args.get('region', '').strip()
    keyword = request.args.get('keyword', '').strip()
    sort_by = request.args.get('sort', 'latest').strip() or 'latest'
    sort_order = request.args.get('order', 'desc').strip().lower() or 'desc'
    page = max(_safe_int(request.args.get('page', 1), 1), 1)
    anchor_index = _safe_int(request.args.get('anchor_index'), None)
    per_page = _safe_int(request.args.get('page_size', 8), 8)

    if per_page < 4:
        per_page = 4
    elif per_page > 12:
        per_page = 12

    reports, representative_reports, _ = _hydrate_reports()

    filtered = []
    for item in reports:
        status = item.get('status') or ''
        if status == '삭제':
            continue
        risk_score = _safe_float(item.get('risk_score'))
        region_name = normalize_region_name(item.get('region_name') or item.get('location') or '')
        title_text = (item.get('title') or '') + ' ' + (item.get('content') or '') + ' ' + (item.get('location') or '')

        if member_id and _safe_int(item.get('user_id')) != member_id:
            continue

        if quick_filter == 'pending' and status not in ('관리자 확인중', '접수완료'):
            continue
        if quick_filter == 'urgent' and not (risk_score >= 80 or _safe_int(item.get('group_reporter_count'), 0) >= 2):
            continue
        if selected_status and status != selected_status:
            continue
        if selected_risk == 'high' and risk_score < 80:
            continue
        if selected_risk == 'medium' and not (50 <= risk_score < 80):
            continue
        if selected_risk == 'low' and risk_score >= 50:
            continue
        if selected_region and region_name != selected_region:
            continue
        if keyword and keyword.lower() not in title_text.lower() and keyword not in str(item.get('id')):
            continue
        filtered.append(item)

    reverse = sort_order != 'asc'
    if sort_by == 'latest':
        filtered.sort(key=lambda x: (x.get('created_at') or datetime.min), reverse=reverse)
    elif sort_by == 'risk':
        filtered.sort(key=lambda x: (_safe_float(x.get('risk_score')), x.get('created_at') or datetime.min), reverse=reverse)
    elif sort_by == 'reports':
        filtered.sort(key=lambda x: (_safe_int(x.get('group_reporter_count'), 0), x.get('created_at') or datetime.min), reverse=reverse)
    elif sort_by == 'status':
        filtered.sort(key=lambda x: (_status_rank(x.get('status')),-_safe_float(x.get('risk_score')),x.get('created_at') or datetime.min),reverse=reverse)
    elif sort_by == 'pending':
        filtered.sort(key=lambda x: (_status_rank(x.get('status')), x.get('created_at') or datetime.min), reverse=(sort_order == 'asc'))
    else:
        sort_by = 'priority'
        filtered.sort(key=lambda x: (_priority_score(x), _safe_float(x.get('risk_score')), x.get('created_at') or datetime.min), reverse=reverse)

    total_count = len(filtered)
    total_pages = max(1, math.ceil(total_count / per_page))

    if anchor_index is not None and anchor_index >= 0:
        page = (anchor_index // per_page) + 1

    if page > total_pages:
        page = total_pages

    start = (page - 1) * per_page
    incidents = filtered[start:start + per_page]

    region_options = sorted({
        normalize_region_name(item.get('region_name') or item.get('location') or '')
        for item in representative_reports
        if normalize_region_name(item.get('region_name') or item.get('location') or '')
    })

    current_query = request.args.to_dict(flat=True)
    current_query.pop('page', None)
    if current_query:
        current_query_string = '&'.join(f"{key}={value}" for key, value in current_query.items() if value != '')
        if current_query_string:
            current_query_string = '&' + current_query_string
    else:
        current_query_string = ''

    return render_template(
        'admin_incidents.html',
        incidents=incidents,
        region_options=region_options,
        selected_region=selected_region,
        selected_status=selected_status,
        selected_risk=selected_risk,
        keyword=keyword,
        sort_by=sort_by,
        sort_order=sort_order,
        quick_filter=quick_filter,
        page=page,
        total_pages=total_pages,
        total_count=total_count,
        current_query_string=current_query_string,
        member_id=member_id,  # 추가
        KAKAO_JS_KEY=current_app.config.get('KAKAO_JS_KEY', ''),
    )

@admin_bp.route('/admin/incidents/group/<int:incident_id>')
def admin_incident_group(incident_id):
    denied = _require_admin()
    if denied:
        return jsonify({'success': False, 'message': '권한이 없습니다.'}), 403

    reports, _, group_map = _hydrate_reports()
    target = next((item for item in reports if _safe_int(item.get('id')) == incident_id), None)

    if not target:
        return jsonify({'success': False, 'message': '신고를 찾을 수 없습니다.'}), 404

    group_ids = group_map.get(incident_id, {}).get('group_ids', [incident_id])
    representative_id = group_map.get(incident_id, {}).get('representative_id')

    group_items = []
    for item in reports:
        if _safe_int(item.get('id')) in group_ids:
            created_at = item.get('created_at')
            group_items.append({
                'id': item.get('id'),
                'title': item.get('title') or '제목 없음',
                'member_name': item.get('nickname') or item.get('username') or f"회원 {item.get('user_id')}",
                'status': item.get('status') or '-',
                'created_at': created_at.strftime('%m-%d %H:%M') if created_at else '-',
                'is_representative': _safe_int(item.get('id')) == _safe_int(representative_id),
            })

    group_items.sort(
        key=lambda x: (0 if x['is_representative'] else 1, x['id'])
    )

    return jsonify({
        'success': True,
        'items': group_items
    })

@admin_bp.route('/incident/update-status', methods=['POST'])
def incident_update_status():
    denied = _require_admin()
    if denied:
        return denied

    if request.is_json:
        payload = request.get_json(silent=True) or {}
        incident_id = _safe_int(payload.get('incident_id'))
        new_status = (payload.get('new_status') or '').strip()
        reject_reason = (payload.get('reject_reason') or '').strip()
    else:
        incident_id = _safe_int(request.form.get('incident_id'))
        new_status = (request.form.get('new_status') or '').strip()
        reject_reason = (request.form.get('reject_reason') or '').strip()

    if not incident_id or new_status not in ('관리자 확인중', '접수완료', '처리중', '처리완료', '처리중', '처리 완료', '반려'):
        if request.is_json:
            return jsonify({'ok': False, 'message': '잘못된 요청입니다.'}), 400
        return redirect(request.referrer or url_for('admin.admin_dashboard'))

    reports, _, group_map = _hydrate_reports()
    target = next((item for item in reports if _safe_int(item.get('id')) == incident_id), None)
    if not target:
        if request.is_json:
            return jsonify({'ok': False, 'message': '신고를 찾을 수 없습니다.'}), 404
        return redirect(request.referrer or url_for('admin.admin_dashboard'))

    target_ids = group_map.get(incident_id, {}).get('group_ids', [incident_id])
    placeholders = ','.join([f':id{i}' for i in range(len(target_ids))])
    params = {f'id{i}': rid for i, rid in enumerate(target_ids)}
    params.update({'new_status': new_status, 'reject_reason': reject_reason if new_status == '반려' else None, 'last_checked_at': datetime.now()})
    sql = text(f"""
        UPDATE report
        SET status = :new_status,
            reject_reason = :reject_reason,
            last_checked_at = :last_checked_at
        WHERE id IN ({placeholders})
    """)
    db.session.execute(sql, params)
    db.session.commit()

    if request.is_json:
        socketio.emit('status_update', {'incident_id': incident_id, 'new_status': new_status}, namespace='/')
        return jsonify({'ok': True, 'message': '상태가 변경되었습니다.'})
    socketio.emit('status_update', {'incident_id': incident_id, 'new_status': new_status}, namespace='/')
    return redirect(request.referrer or url_for('admin.admin_dashboard'))

@admin_bp.route('/admin/incidents/bulk-update', methods=['POST'])
def bulk_update_incidents():
    denied = _require_admin()
    if denied:
        return denied

    incident_ids = request.form.getlist('incident_ids')
    new_status = (request.form.get('new_status') or '').strip()
    reject_reason = (request.form.get('reject_reason') or '').strip()
    return_query = (request.form.get('return_query') or '').strip()

    if not incident_ids:
        return redirect(f"/admin/incidents?{return_query}" if return_query else url_for('admin.admin_incidents'))

    if new_status not in ('관리자 확인중', '접수완료', '처리중', '처리완료', '반려'):
        return redirect(f"/admin/incidents?{return_query}" if return_query else url_for('admin.admin_incidents'))

    incident_ids = [_safe_int(i) for i in incident_ids if _safe_int(i) > 0]
    if not incident_ids:
        return redirect(f"/admin/incidents?{return_query}" if return_query else url_for('admin.admin_incidents'))

    reports, _, group_map = _hydrate_reports()

    target_ids = set()
    for incident_id in incident_ids:
        grouped_ids = group_map.get(incident_id, {}).get('group_ids', [incident_id])
        for rid in grouped_ids:
            target_ids.add(_safe_int(rid))

    target_ids = [rid for rid in target_ids if rid > 0]
    if not target_ids:
        return redirect(f"/admin/incidents?{return_query}" if return_query else url_for('admin.admin_incidents'))

    placeholders = ','.join([f':id{i}' for i in range(len(target_ids))])
    params = {f'id{i}': rid for i, rid in enumerate(target_ids)}
    params.update({
        'new_status': new_status,
        'reject_reason': reject_reason if new_status == '반려' else None,
        'last_checked_at': datetime.now()
    })

    sql = text(f"""
        UPDATE report
        SET status = :new_status,
            reject_reason = :reject_reason,
            last_checked_at = :last_checked_at
        WHERE id IN ({placeholders})
    """)
    db.session.execute(sql, params)
    db.session.commit()

    for rid in target_ids:
        socketio.emit('status_update', {'incident_id': _safe_int(rid), 'new_status': new_status}, namespace='/')

    return redirect(f"/admin/incidents?{return_query}" if return_query else url_for('admin.admin_incidents'))


# [NEW] AI 재분석 엔드포인트 (alert_view_v2.html의 reAnalyzeAI 함수에서 호출)
@admin_bp.route('/api/admin/report/<int:report_id>/reanalyze', methods=['POST'])
def admin_reanalyze_report(report_id):
    denied = _require_admin()
    if denied:
        return jsonify({'success': False, 'message': '권한이 없습니다.'}), 403

    import threading
    from models import Report, AiResult

    report = Report.query.get(report_id)
    if not report:
        return jsonify({'success': False, 'message': '신고를 찾을 수 없습니다.'}), 404

    if not report.file_path:
        return jsonify({'success': False, 'message': '분석할 파일이 없습니다.'}), 400

    # 기존 AI 결과 삭제
    existing_ai = AiResult.query.filter_by(report_id=report_id).all()
    for ai in existing_ai:
        db.session.delete(ai)

    # 상태를 'AI 분석중'으로 변경
    report.status = '관리자 확인중'
    db.session.commit()

    # 파일 타입 판별
    ext_video = (report.file_path or '').lower().endswith(('.mp4', '.mov', '.avi', '.m4v'))
    file_type = 'video' if ext_video else (report.file_type or 'image')

    # AI 분석을 백그라운드 스레드로 실행 (app.run_ai_analysis 사용)
    run_ai = current_app.run_ai_analysis
    thread = threading.Thread(target=run_ai, args=(report_id, report.file_path, file_type))
    thread.start()

    return jsonify({'success': True, 'message': 'AI 재분석이 시작되었습니다.'})


@admin_bp.route('/admin/members')
def admin_members():
    denied = _require_admin()
    if denied:
        return denied

    keyword = request.args.get('keyword', '').strip()
    role = request.args.get('role', '').strip()
    sort = request.args.get('sort', 'role').strip() or 'role'
    order = request.args.get('order', 'asc').strip().lower() or 'asc'
    page = max(_safe_int(request.args.get('page', 1), 1), 1)
    anchor_index = _safe_int(request.args.get('anchor_index'), None)
    per_page = _safe_int(request.args.get('page_size', 10), 10)

    if per_page < 4:
        per_page = 4
    elif per_page > 15:
        per_page = 15

    sql = text("""
        SELECT
            id,
            username,
            nickname,
            created_at,
            is_admin,
            active,
            manager_region,
            email,
            COALESCE(role, CASE WHEN is_admin = 1 THEN 'admin' ELSE 'user' END) AS role
        FROM members
        ORDER BY id DESC
    """)
    rows = [dict(r) for r in db.session.execute(sql).mappings().all()]

    members = []
    for row in rows:
        item = dict(row)
        item['name'] = _member_name(row)
        item['uid'] = _member_uid(row)
        item['created_at'] = _parse_dt(row.get('created_at'))
        members.append(item)

    if keyword:
        members = [m for m in members if keyword.lower() in (m.get('name') or '').lower() or keyword.lower() in (m.get('uid') or '').lower() or keyword == str(m.get('id'))]
    if role:
        members = [m for m in members if (m.get('role') or '') == role]

    reverse = order == 'desc'
    if sort == 'name':
        members.sort(key=lambda x: (x.get('name') or '').lower(), reverse=reverse)
    elif sort == 'uid':
        members.sort(key=lambda x: (x.get('uid') or '').lower(), reverse=reverse)
    elif sort == 'created_at':
        members.sort(key=lambda x: x.get('created_at') or datetime.min, reverse=reverse)
    elif sort == 'active':
        members.sort(key=lambda x: (_safe_int(x.get('active')), x.get('id')), reverse=reverse)
    elif sort == 'id':
        members.sort(key=lambda x: _safe_int(x.get('id')), reverse=reverse)
    else:
        sort = 'role'
        rank = {'admin': 1, 'manager': 2, 'user': 3}
        members.sort(key=lambda x: (rank.get(x.get('role') or 'user', 99), (x.get('name') or '').lower()), reverse=reverse)

    total_pages = max(1, math.ceil(len(members) / per_page))

    if anchor_index is not None and anchor_index >= 0:
        page = (anchor_index // per_page) + 1

    if page > total_pages:
        page = total_pages

    members = members[(page - 1) * per_page: page * per_page]

    return render_template(
        'admin_members.html',
        members=members,
        keyword=keyword,
        role=role,
        sort=sort,
        order=order,
        page=page,
        total_pages=total_pages,
    )


# [NOTICE] 상세페이지(member_detail)에서는 모바일 브라우저의 PTR(Pull-to-Refresh) 기능을
# layout.html의 스크립트를 통해 '하드하게' 차단하고 있습니다.
# 이는 카카오 지도 로더와의 충돌을 방지하기 위함이므로, 상세페이지 레이아웃 유지 시 주의하십시오.
@admin_bp.route('/admin/members/<int:member_id>')
def admin_member_detail(member_id):
    denied = _require_admin()
    if denied:
        return denied

    member_row = db.session.execute(text("""
        SELECT
            id,
            username,
            nickname,
            created_at,
            is_admin,
            active,
            manager_region,
            email,
            COALESCE(role, CASE WHEN is_admin = 1 THEN 'admin' ELSE 'user' END) AS role
        FROM members
        WHERE id = :member_id
        LIMIT 1
    """), {'member_id': member_id}).mappings().first()
    if not member_row:
        return redirect(url_for('admin.admin_members'))

    member = dict(member_row)
    member['name'] = _member_name(member)
    member['uid'] = _member_uid(member)
    member['created_at'] = _parse_dt(member.get('created_at'))

    reports, _, group_map = _hydrate_reports()
    member_reports = [r for r in reports if _safe_int(r.get('user_id')) == member_id]

    total = len(member_reports)
    received = sum(1 for r in member_reports if (r.get('status') or '') == '접수완료')
    processing = sum(1 for r in member_reports if (r.get('status') or '') == '처리중')
    completed = sum(1 for r in member_reports if (r.get('status') or '') == '처리완료')
    rejected = sum(1 for r in member_reports if (r.get('status') or '') == '반려')
    pending = sum(1 for r in member_reports if (r.get('status') or '') in ('관리자 확인중', '접수완료', '처리중'))
    high_risk_pending = sum(1 for r in member_reports if (r.get('status') or '') in ('관리자 확인중', '접수완료', '처리중') and _safe_float(r.get('risk_score')) >= 80)
    long_pending = sum(1 for r in member_reports if (r.get('status') or '') in ('관리자 확인중', '접수완료') and r.get('created_at') and (datetime.now() - r['created_at']).total_seconds() >= 86400)
    recent_7d = sum(1 for r in member_reports if r.get('created_at') and (datetime.now() - r['created_at']).days < 7)
    recent_30d = sum(1 for r in member_reports if r.get('created_at') and (datetime.now() - r['created_at']).days < 30)
    approved_rate = round((completed / total) * 100, 1) if total else 0
    rejected_rate = round((rejected / total) * 100, 1) if total else 0
    duplicate_count = sum(1 for r in member_reports if _safe_int(r.get('group_reporter_count'), 0) >= 1)
    duplicate_rate = round((duplicate_count / total) * 100, 1) if total else 0

    member_stats = {
        'total_reports': total,
        'received_reports': received,
        'processing_reports': processing,
        'completed_reports': completed,
        'rejected_reports': rejected,
        'pending_reports': pending,
        'high_risk_pending_reports': high_risk_pending,
        'long_pending_reports': long_pending,
        'recent_7d_reports': recent_7d,
        'recent_30d_reports': recent_30d,
        'approved_rate': approved_rate,
        'rejected_rate': rejected_rate,
        'duplicate_rate': duplicate_rate,
    }

    latest_posts = sorted(member_reports, key=lambda x: x.get('created_at') or datetime.min, reverse=True)[:4]

    summary_parts = []
    if recent_30d >= 5:
        summary_parts.append('최근 30일 활동 많음')
    if rejected_rate >= 40:
        summary_parts.append('반려 비율 높음')
    if duplicate_rate <= 20 and total > 0:
        summary_parts.append('중복 신고 낮음')
    if not summary_parts:
        summary_parts.append('기본 활동 상태')
    member_summary_comment = ' · '.join(summary_parts)

    return render_template(
        'admin_member_detail.html',
        member=member,
        member_stats=member_stats,
        member_incidents=latest_posts,
        member_summary_comment=member_summary_comment,
    )

def _member_detail_redirect(member_id):
    page = request.form.get('page', request.args.get('page', 1))
    keyword = request.form.get('keyword', request.args.get('keyword', ''))
    role = request.form.get('role_filter', request.args.get('role', ''))
    sort = request.form.get('sort', request.args.get('sort', 'role'))
    order = request.form.get('order', request.args.get('order', 'asc'))

    return redirect(url_for(
        'admin.admin_member_detail',
        member_id=member_id,
        page=page,
        keyword=keyword,
        role=role,
        sort=sort,
        order=order
    ))


@admin_bp.route('/admin/members/<int:member_id>/role', methods=['POST'])
def admin_member_change_role(member_id):
    denied = _require_admin()
    if denied:
        return denied

    new_role = (request.form.get('role') or '').strip()
    if new_role not in ('admin', 'manager', 'user'):
        return _member_detail_redirect(member_id)

    db.session.execute(
        text("UPDATE members SET role = :role WHERE id = :member_id"),
        {'role': new_role, 'member_id': member_id}
    )
    db.session.commit()

    return _member_detail_redirect(member_id)


@admin_bp.route('/admin/members/<int:member_id>/suspend', methods=['POST'])
def admin_member_suspend(member_id):
    denied = _require_admin()
    if denied:
        return denied

    db.session.execute(
        text("UPDATE members SET active = 0 WHERE id = :member_id"),
        {'member_id': member_id}
    )
    db.session.commit()

    return _member_detail_redirect(member_id)


@admin_bp.route('/admin/members/<int:member_id>/unsuspend', methods=['POST'])
def admin_member_unsuspend(member_id):
    denied = _require_admin()
    if denied:
        return denied

    db.session.execute(
        text("UPDATE members SET active = 1 WHERE id = :member_id"),
        {'member_id': member_id}
    )
    db.session.commit()

    return _member_detail_redirect(member_id)


def add_to_region_tree(tree: dict, parts: list[str]):
    if not parts:
        return

    node = tree
    for i, part in enumerate(parts):
        is_last = i == len(parts) - 1

        if is_last:
            current = node.get(part, 0)

            if isinstance(current, dict):
                current["__count__"] = current.get("__count__", 0) + 1
            else:
                node[part] = current + 1

        else:
            current = node.get(part)

            if current is None:
                node[part] = {}
            elif isinstance(current, int):
                node[part] = {"__count__": current}
            elif not isinstance(current, dict):
                node[part] = {}

            node = node[part]

@admin_bp.route('/admin/statistics')
def admin_statistics():
    denied = _require_admin()
    if denied:
        return denied

    reports, _, _ = _hydrate_reports()
    now = datetime.now()

    # -----------------------------
    # 1) 지역별 계층 집계
    # -----------------------------
    region_data_map = {"all": {}}

    for r in reports:
        raw_address = r.get('region_name') or r.get('location') or ''
        parts = parse_region_hierarchy(raw_address)

        if not parts:
            add_to_region_tree(region_data_map["all"], ["기타"])
            continue

        add_to_region_tree(region_data_map["all"], parts)

    # -----------------------------
    # 2) 기간별 추이 데이터 생성 헬퍼
    # -----------------------------
    def build_period_bundle(days: int):
        labels = []
        values = []
        previous_values = []

        # 현재 기간
        current_start = (now - timedelta(days=days - 1)).date()
        current_dates = [current_start + timedelta(days=i) for i in range(days)]

        # 이전 동일 기간
        prev_start = current_start - timedelta(days=days)
        prev_dates = [prev_start + timedelta(days=i) for i in range(days)]

        current_map = {d: 0 for d in current_dates}
        prev_map = {d: 0 for d in prev_dates}

        for r in reports:
            created_at = r.get('created_at')
            if not created_at:
                continue

            d = created_at.date()
            if d in current_map:
                current_map[d] += 1
            if d in prev_map:
                prev_map[d] += 1

        for d in current_dates:
            if days == 7:
                labels.append(d.strftime('%m/%d'))
            else:
                labels.append(d.strftime('%m/%d'))
            values.append(current_map[d])

        for d in prev_dates:
            previous_values.append(prev_map[d])

        return {
            "labels": labels,
            "values": values,
            "previous_values": previous_values
        }

    # -----------------------------
    # 3) 전체 추이 데이터
    # -----------------------------
    dated_reports = [r for r in reports if r.get('created_at')]
    dated_reports.sort(key=lambda x: x.get('created_at') or datetime.min)

    if dated_reports:
        first_date = dated_reports[0]['created_at'].date()
        last_date = dated_reports[-1]['created_at'].date()

        all_dates = []
        cursor = first_date
        while cursor <= last_date:
            all_dates.append(cursor)
            cursor += timedelta(days=1)

        all_map = {d: 0 for d in all_dates}
        for r in dated_reports:
            all_map[r['created_at'].date()] += 1

        all_labels = [d.strftime('%m/%d') for d in all_dates]
        all_values = [all_map[d] for d in all_dates]
    else:
        all_labels = []
        all_values = []

    trend_data_map = {
        "all": {
            "7d": build_period_bundle(7),
            "30d": build_period_bundle(30),
            "all": {
                "labels": all_labels,
                "values": all_values,
                "previous_values": []
            }
        }
    }

    # -----------------------------
    # 4) 상단 요약
    # -----------------------------
    total_reports = len(reports)
    pending_count = sum(
        1 for r in reports
        if (r.get('status') or '') in ('관리자 확인중', '접수완료')
    )
    danger_count = sum(
        1 for r in reports
        if _safe_float(r.get('risk_score')) >= 80
    )
    processing_count = sum(
        1 for r in reports
        if (r.get('status') or '') == '처리중'
    )
    today_count = sum(
        1 for r in reports
        if r.get('created_at') and r['created_at'].date() == now.date()
    )

    statistics_summary = {
        "total_reports": total_reports,
        "pending_count": pending_count,
        "danger_count": danger_count,
        "processing_count": processing_count,
        "today_count": today_count,
    }

    return render_template(
        'admin_statistics.html',
        region_data_map=region_data_map,
        trend_data_map=trend_data_map,
        statistics_summary=statistics_summary,
        page=1,
        total_pages=1,
        total_count=total_reports,
    )

@admin_bp.route('/admin/ppt')
def admin_ppt():
    denied = _require_admin()
    if denied:
        return denied
    return render_template('ppt.html')

@admin_bp.route('/admin/ppt/spot-<int:num>')
def admin_ppt_spot(num):
    denied = _require_admin()
    if denied:
        return denied
    return render_template(f'ppt/spot-{num}.html')
