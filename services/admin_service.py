import math
from collections import defaultdict
from datetime import datetime, timedelta

from services.region_service import normalize_region_name, parse_region_hierarchy
from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, current_app
from sqlalchemy import or_, text

from extensions import db, socketio
from models import Report, AiResult, Member
from services.report_workflow import normalize_status, transition_report

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


def _display_location(address, region_name):
    """관리자 목록에는 도로명 전체가 아닌 시·군·구 단위 위치를 표시한다."""
    raw_location = address or region_name or ''
    hierarchy = parse_region_hierarchy(raw_location)
    if hierarchy and hierarchy != ['기타']:
        return ' '.join(hierarchy)
    return raw_location or '위치 정보 없음'


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
    return session.get('user_role', 'user')


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

        item['location'] = _display_location(item.get('address'), item.get('region_name'))
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
                both_open = (
                    (current.get('status') or '') in ('접수완료', '처리중')
                    and (other.get('status') or '') in ('접수완료', '처리중')
                )
                if not both_open and abs((current_dt - other_dt).total_seconds()) > 86400:
                    continue
                if haversine_m(current.get('latitude'), current.get('longitude'), other.get('latitude'), other.get('longitude')) > 30:
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
            if created_at and status == '접수완료' and (datetime.now() - created_at).total_seconds() >= 86400:
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

    # 접수완료는 모든 신고의 기본 시작 상태이므로 긴급도 가점을 주지 않는다.
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

    if created_at and status == '접수완료' and (datetime.now() - created_at).total_seconds() >= 86400:
        score += 40

    return score


def _status_rank(status):
    order = {
        '접수완료': 0,
        '처리중': 1,
        '처리완료': 2,
        '삭제': 3,
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
        return (item.get('status') or '') == '접수완료'

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
        'completed_count': sum(1 for item in reports if (item.get('status') or '') == '처리완료'),
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
    elif selected_tab == 'completed':
        dashboard_items = [item for item in reports if (item.get('status') or '') == '처리완료']
        dashboard_section_title = '처리완료'
        dashboard_section_subtitle = '처리가 완료된 신고 목록입니다.'
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

    # 전체 관리 화면은 같은 장소의 제보를 사고 대표 건으로 표시한다.
    # 회원 상세에서 진입한 경우에는 해당 회원이 실제로 남긴 제보 기록을 유지한다.
    source_reports = reports if member_id else representative_reports

    filtered = []
    for item in source_reports:
        status = item.get('status') or ''
        if status == '삭제':
            continue
        risk_score = _safe_float(item.get('risk_score'))
        region_name = normalize_region_name(item.get('region_name') or item.get('location') or '')
        title_text = (item.get('title') or '') + ' ' + (item.get('content') or '') + ' ' + (item.get('location') or '')

        if member_id and _safe_int(item.get('user_id')) != member_id:
            continue

        if quick_filter == 'pending' and status != '접수완료':
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

    try:
        new_status = normalize_status(new_status)
    except ValueError:
        new_status = None
    if not incident_id or not new_status:
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
    try:
        rewarded_users = set()
        grouped_reports = Report.query.filter(Report.id.in_(target_ids)).order_by(Report.id.asc()).all()
        for report in grouped_reports:
            award_points = report.user_id not in rewarded_users
            transition_report(report, new_status, reject_reason, award_points=award_points)
            if report.user_id is not None:
                rewarded_users.add(report.user_id)
        db.session.commit()
    except ValueError as exc:
        db.session.rollback()
        if request.is_json:
            return jsonify({'ok': False, 'message': str(exc)}), 400
        return redirect(request.referrer or url_for('admin.admin_dashboard'))

    if request.is_json:
        socketio.emit(
            'status_update', {'incident_id': incident_id, 'new_status': new_status},
            namespace='/', to='authenticated'
        )
        return jsonify({'ok': True, 'message': '상태가 변경되었습니다.'})
    socketio.emit(
        'status_update', {'incident_id': incident_id, 'new_status': new_status},
        namespace='/', to='authenticated'
    )
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

    try:
        new_status = normalize_status(new_status)
    except ValueError:
        return redirect(f"/admin/incidents?{return_query}" if return_query else url_for('admin.admin_incidents'))

    incident_ids = [_safe_int(i) for i in incident_ids if _safe_int(i) > 0]
    if not incident_ids:
        return redirect(f"/admin/incidents?{return_query}" if return_query else url_for('admin.admin_incidents'))

    reports, _, group_map = _hydrate_reports()

    target_groups = []
    seen_groups = set()
    for incident_id in incident_ids:
        grouped_ids = group_map.get(incident_id, {}).get('group_ids', [incident_id])
        normalized_group = tuple(sorted({
            _safe_int(rid) for rid in grouped_ids if _safe_int(rid) > 0
        }))
        if normalized_group and normalized_group not in seen_groups:
            seen_groups.add(normalized_group)
            target_groups.append(normalized_group)

    target_ids = sorted({rid for group in target_groups for rid in group})
    if not target_ids:
        return redirect(f"/admin/incidents?{return_query}" if return_query else url_for('admin.admin_incidents'))

    try:
        reports_by_id = {
            report.id: report
            for report in Report.query.filter(Report.id.in_(target_ids)).all()
        }
        for group_ids in target_groups:
            rewarded_users = set()
            for report_id in group_ids:
                report = reports_by_id.get(report_id)
                if report is None:
                    continue
                award_points = report.user_id not in rewarded_users
                transition_report(report, new_status, reject_reason, award_points=award_points)
                if report.user_id is not None:
                    rewarded_users.add(report.user_id)
        db.session.commit()
    except ValueError:
        db.session.rollback()
        return redirect(f"/admin/incidents?{return_query}" if return_query else url_for('admin.admin_incidents'))

    for rid in target_ids:
        socketio.emit(
            'status_update', {'incident_id': _safe_int(rid), 'new_status': new_status},
            namespace='/', to='authenticated'
        )

    return redirect(f"/admin/incidents?{return_query}" if return_query else url_for('admin.admin_incidents'))


# [NEW] AI 재분석 엔드포인트 (alert_view_v2.html의 reAnalyzeAI 함수에서 호출)
@admin_bp.route('/api/admin/report/<int:report_id>/reanalyze', methods=['POST'])
def admin_reanalyze_report(report_id):
    denied = _require_admin()
    if denied:
        return jsonify({'success': False, 'message': '권한이 없습니다.'}), 403

    report = db.session.get(Report, report_id)
    if not report:
        return jsonify({'success': False, 'message': '신고를 찾을 수 없습니다.'}), 404

    if not report.file_path:
        return jsonify({'success': False, 'message': '분석할 파일이 없습니다.'}), 400
    if not current_app.config.get('AI_AVAILABLE', False):
        return jsonify({'success': False, 'message': 'AI 모델이 설정되지 않아 재분석할 수 없습니다.'}), 503

    # 기존 AI 결과 삭제
    existing_ai = AiResult.query.filter_by(report_id=report_id).all()
    for ai in existing_ai:
        db.session.delete(ai)

    db.session.commit()

    # 파일 타입 판별
    ext_video = (report.file_path or '').lower().endswith(('.mp4', '.mov', '.avi', '.m4v'))
    file_type = 'video' if ext_video else (report.file_type or 'image')

    # AI 분석을 백그라운드 스레드로 실행 (app.run_ai_analysis 사용)
    if not current_app.submit_ai_analysis(report_id, report.file_path, file_type):
        return jsonify({'success': False, 'message': 'AI 분석 대기열이 가득 찼습니다.'}), 503

    return jsonify({'success': True, 'message': 'AI 재분석이 시작되었습니다.'})


@admin_bp.route('/admin/members')
def admin_members():
    denied = _require_admin()
    if denied:
        return denied

    keyword = request.args.get('keyword', '').strip()
    role = request.args.get('role', '').strip()
    status = request.args.get('status', '').strip()
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
            address,
            region_city,
            region_district,
            COALESCE(points, 0) AS points,
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

    member_summary = {
        'total': len(members),
        'active': sum(1 for member in members if _safe_int(member.get('active')) == 1),
        'suspended': sum(1 for member in members if _safe_int(member.get('active')) != 1),
        'staff': sum(1 for member in members if (member.get('role') or '') in ('admin', 'manager')),
    }

    if keyword:
        members = [
            member for member in members
            if keyword.lower() in (member.get('name') or '').lower()
            or keyword.lower() in (member.get('uid') or '').lower()
            or keyword.lower() in (member.get('email') or '').lower()
            or keyword == str(member.get('id'))
        ]
    if role == 'staff':
        members = [m for m in members if (m.get('role') or '') in ('admin', 'manager')]
    elif role:
        members = [m for m in members if (m.get('role') or '') == role]
    if status == 'active':
        members = [m for m in members if _safe_int(m.get('active')) == 1]
    elif status == 'suspended':
        members = [m for m in members if _safe_int(m.get('active')) != 1]

    reverse = order == 'desc'
    if sort == 'name':
        members.sort(key=lambda x: (x.get('name') or '').lower(), reverse=reverse)
    elif sort == 'uid':
        members.sort(key=lambda x: (x.get('uid') or '').lower(), reverse=reverse)
    elif sort == 'email':
        members.sort(key=lambda x: (x.get('email') or '').lower(), reverse=reverse)
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

    total_count = len(members)
    total_pages = max(1, math.ceil(total_count / per_page))

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
        status=status,
        sort=sort,
        order=order,
        page=page,
        total_pages=total_pages,
        total_count=total_count,
        member_summary=member_summary,
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
    pending = sum(1 for r in member_reports if (r.get('status') or '') in ('접수완료', '처리중'))
    high_risk_pending = sum(1 for r in member_reports if (r.get('status') or '') in ('접수완료', '처리중') and _safe_float(r.get('risk_score')) >= 80)
    long_pending = sum(1 for r in member_reports if (r.get('status') or '') == '접수완료' and r.get('created_at') and (datetime.now() - r['created_at']).total_seconds() >= 86400)
    recent_7d = sum(1 for r in member_reports if r.get('created_at') and (datetime.now() - r['created_at']).days < 7)
    recent_30d = sum(1 for r in member_reports if r.get('created_at') and (datetime.now() - r['created_at']).days < 30)
    approved_rate = round((completed / total) * 100, 1) if total else 0
    duplicate_count = sum(1 for r in member_reports if _safe_int(r.get('group_reporter_count'), 0) >= 1)
    duplicate_rate = round((duplicate_count / total) * 100, 1) if total else 0

    member_stats = {
        'total_reports': total,
        'received_reports': received,
        'processing_reports': processing,
        'completed_reports': completed,
        'pending_reports': pending,
        'high_risk_pending_reports': high_risk_pending,
        'long_pending_reports': long_pending,
        'recent_7d_reports': recent_7d,
        'recent_30d_reports': recent_30d,
        'approved_rate': approved_rate,
        'duplicate_rate': duplicate_rate,
    }

    latest_posts = sorted(member_reports, key=lambda x: x.get('created_at') or datetime.min, reverse=True)[:4]

    summary_parts = []
    if recent_30d >= 5:
        summary_parts.append('최근 30일 활동 많음')
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
    status = request.form.get('status_filter', request.args.get('status', ''))
    sort = request.form.get('sort', request.args.get('sort', 'role'))
    order = request.form.get('order', request.args.get('order', 'asc'))

    return redirect(url_for(
        'admin.admin_member_detail',
        member_id=member_id,
        page=page,
        keyword=keyword,
        role=role,
        status=status,
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
    if member_id == session.get('user_id') and new_role != 'admin':
        return _member_detail_redirect(member_id)
    target = db.session.get(Member, member_id)
    if not target:
        return _member_detail_redirect(member_id)
    if (target.is_admin or target.role == 'admin') and new_role != 'admin':
        admin_count = Member.query.filter(or_(Member.is_admin.is_(True), Member.role == 'admin')).count()
        if admin_count <= 1:
            return _member_detail_redirect(member_id)

    db.session.execute(
        text("UPDATE members SET role = :role, is_admin = :is_admin WHERE id = :member_id"),
        {'role': new_role, 'is_admin': new_role == 'admin', 'member_id': member_id}
    )
    db.session.commit()

    return _member_detail_redirect(member_id)


@admin_bp.route('/admin/members/<int:member_id>/suspend', methods=['POST'])
def admin_member_suspend(member_id):
    denied = _require_admin()
    if denied:
        return denied
    if member_id == session.get('user_id'):
        return _member_detail_redirect(member_id)

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
    region_report_details = {}

    for r in reports:
        raw_address = r.get('region_name') or r.get('location') or ''
        parts = parse_region_hierarchy(raw_address)

        if not parts:
            add_to_region_tree(region_data_map["all"], ["기타"])
            continue

        add_to_region_tree(region_data_map["all"], parts)

        created_at = r.get('created_at')
        report_detail = {
            "id": r.get('id'),
            "title": r.get('title') or f"신고 #{r.get('id')}",
            "location": _display_location(r.get('address'), r.get('region_name')),
            "status": r.get('status') or '접수완료',
            "ai_score": round(_safe_float(r.get('risk_score'))),
            "damage_type": r.get('damage_type') or '분류 전',
            "created_at": created_at.strftime('%Y-%m-%d %H:%M') if created_at else '-',
        }

        # 전국 → 시·도 → 시·군·구 어느 단계에서도 같은 신고를 집계할 수
        # 있도록 각 상위 경로별 상세 데이터를 함께 만든다.
        for depth in range(1, len(parts) + 1):
            region_key = '||'.join(parts[:depth])
            region_report_details.setdefault(region_key, []).append(report_detail)

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

    return render_template(
        'admin_statistics.html',
        region_data_map=region_data_map,
        region_report_details=region_report_details,
        trend_data_map=trend_data_map,
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
