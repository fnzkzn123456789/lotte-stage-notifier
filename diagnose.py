"""알림 전송 없이 실제 롯데시네마 연결을 확인한다."""
import json
from dataclasses import asdict
from pathlib import Path

from notifier.core import LotteClient, is_stage, now_kst, parse_screenings


def main():
    client = LotteClient()
    cinemas = client.cinemas()
    events = client.events()
    print(f"영화관 {len(cinemas)}곳 / 이벤트 {len(events)}건 조회", flush=True)
    targets = set()
    for event in events:
        for row in client.event_rows(str(event["EventID"])):
            cid, day = str(row.get("CinemaID", "")), str(row.get("PlayDate", ""))[:10]
            if cid in cinemas and is_stage(row) and day >= now_kst().date().isoformat():
                targets.add((cid, day))
    shows = []
    print(f"상영시간표 {len(targets)}건 검증 중", flush=True)
    for cid, day in sorted(targets):
        shows.extend(parse_screenings(client.schedule(cinemas[cid], day), cinemas))
    output = Path(__file__).parent / "data" / "live-check.json"
    output.parent.mkdir(exist_ok=True)
    result = {"checked_at": now_kst().isoformat(), "cinema_count": len(cinemas),
              "event_count": len(events), "schedule_count": len(targets),
              "screening_count": len(shows), "bookable_count": sum(s.bookable for s in shows),
              "screenings": [asdict(s) for s in shows]}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "screenings"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
