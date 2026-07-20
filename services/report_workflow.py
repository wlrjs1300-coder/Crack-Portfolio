"""신고 상태 전이와 포인트 지급을 한 곳에서 일관되게 처리한다."""

from extensions import db
from models import Member, PointLog, Report
from sqlalchemy.exc import IntegrityError
from utils import get_now_kst


STATUS_ALIASES = {
    '담당자 확인중': '관리자 확인중',
    '신고 처리중': '처리중',
    '처리 완료': '처리완료',
}
VALID_STATUSES = {'AI 분석중', '관리자 확인중', '접수완료', '처리중', '처리완료', '반려', '삭제'}


def normalize_status(status):
    normalized = STATUS_ALIASES.get((status or '').strip(), (status or '').strip())
    if normalized not in VALID_STATUSES:
        raise ValueError('허용되지 않은 신고 상태입니다.')
    return normalized


def transition_report(report, new_status, reject_reason=None):
    """상태를 변경하고 신고별 포인트 로그를 이용해 중복 지급/차감을 막는다."""
    normalized = normalize_status(new_status)
    report.status = normalized
    report.reject_reason = reject_reason if normalized == '반려' else None
    report.last_checked_at = get_now_kst()

    if not report.user_id:
        return normalized
    member = db.session.get(Member, report.user_id)
    if not member:
        return normalized

    if normalized == '처리완료':
        reason = f'신고 #{report.id} 처리 완료 보상'
        if _create_point_event_once(
            report.user_id, 20, reason, f'report:{report.id}:completed'
        ):
            member.points = (member.points or 0) + 20
    elif normalized == '반려':
        reason = f'신고 #{report.id} 반려 차감'
        deduction = min(10, max(0, member.points or 0))
        if _create_point_event_once(
            report.user_id, -deduction, reason, f'report:{report.id}:rejected'
        ):
            member.points = (member.points or 0) - deduction
    return normalized


def _create_point_event_once(user_id, amount, reason, idempotency_key):
    """고유 키 삽입을 savepoint에서 시도해 동시 요청의 중복 포인트를 막는다."""
    try:
        with db.session.begin_nested():
            db.session.add(PointLog(
                user_id=user_id, amount=amount, reason=reason,
                idempotency_key=idempotency_key,
            ))
            db.session.flush()
        return True
    except IntegrityError:
        return False


def backfill_legacy_statuses():
    """기존 DB에 남은 상태 별칭을 canonical 값으로 한 번 정리한다."""
    changed = 0
    for legacy, canonical in STATUS_ALIASES.items():
        changed += Report.query.filter_by(status=legacy).update(
            {'status': canonical}, synchronize_session=False
        )
    if changed:
        db.session.commit()
    return changed
