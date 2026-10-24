"""평가 하니스 진행 상황을 브라우저로 본다 (읽기 전용, 5초마다 새로 고침).

    python bench/watch.py            # http://127.0.0.1:8790

하니스는 서버 없이 돌기 때문에, 하니스가 남기는 파일만 읽는다.
* 끝난 실행: bench/results/에서 가장 최근 .jsonl
* 지금 실행 중인 대화: 하니스 격리 폴더(%TEMP%/triagent-tests-*)의 roundtable_log.md
"""

from __future__ import annotations

import html
import json
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RESULTS_DIR = Path(__file__).resolve().parent / "results"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8790
ARM_LABELS = {"solo": "Claude 단독", "pipeline": "3명 파이프라인"}


def newest(paths):
    paths = [path for path in paths if path.is_file()]
    return max(paths, key=lambda path: path.stat().st_mtime) if paths else None


def results_rows() -> tuple[str, list[dict]]:
    path = newest(RESULTS_DIR.glob("*.jsonl"))
    if not path:
        return "", []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return path.name, rows


def live_log(max_sections: int = 6) -> tuple[str, str]:
    path = newest(Path(tempfile.gettempdir()).glob("triagent-tests-*/roundtable_log.md"))
    if not path:
        return "", ""
    sections = path.read_text(encoding="utf-8", errors="replace").split("\n### ")
    return str(path.parent), "\n### ".join(sections[-max_sections:])


def page() -> str:
    name, rows = results_rows()
    folder, log = live_log()
    body_rows = "".join(
        f"<tr><td>{html.escape(r['task'])}</td><td>{ARM_LABELS.get(r['arm'], r['arm'])}</td>"
        f"<td class={'ok' if r['success'] else 'fail'}>{'성공' if r['success'] else '실패'}</td>"
        f"<td>{r['hidden_passed']}/{r['hidden_total']}</td><td>{r['seconds']:.0f}초</td>"
        f"<td>{r['calls']}</td><td>{html.escape(r['outcome'])}</td></tr>"
        for r in rows
    ) or '<tr><td colspan="7">아직 끝난 실행이 없습니다.</td></tr>'
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="5">
<title>평가 하니스 진행</title>
<style>
:root {{ --bg:#fafaf9; --fg:#1c1917; --muted:#78716c; --line:#e7e5e4; --ok:#15803d; --fail:#b91c1c; --code:#f5f5f4; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#1c1917; --fg:#f5f5f4; --muted:#a8a29e; --line:#44403c; --ok:#4ade80; --fail:#f87171; --code:#292524; }} }}
body {{ margin:0; padding:24px 16px; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, "Malgun Gothic", sans-serif; }}
main {{ max-width:960px; margin:0 auto; }}
h1 {{ font-size:20px; margin:0 0 4px; }} h2 {{ font-size:16px; margin:28px 0 8px; }}
.muted {{ color:var(--muted); font-size:13px; }}
table {{ width:100%; border-collapse:collapse; }} th, td {{ text-align:left; padding:6px 8px; border-bottom:1px solid var(--line); }}
th {{ font-size:13px; color:var(--muted); font-weight:600; }}
.ok {{ color:var(--ok); font-weight:600; }} .fail {{ color:var(--fail); font-weight:600; }}
pre {{ background:var(--code); padding:12px; border-radius:6px; white-space:pre-wrap; word-break:break-word; font-size:13px; }}
.scroll {{ overflow-x:auto; }}
</style></head><body><main>
<h1>평가 하니스 진행 상황</h1>
<p class="muted">5초마다 새로 고침 · 결과 파일: {html.escape(name or "없음")}</p>
<h2>끝난 실행 ({len(rows)}개)</h2>
<div class="scroll"><table><thead><tr><th>과제</th><th>방식</th><th>결과</th><th>숨김 테스트</th><th>시간</th><th>모델 호출</th><th>종료</th></tr></thead>
<tbody>{body_rows}</tbody></table></div>
<h2>지금 진행 중인 대화 (최근 기록)</h2>
<p class="muted">{html.escape(folder or "실행 중인 하니스가 없습니다")}</p>
<pre>{html.escape(log or "기록 없음")}</pre>
</main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = page().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


if __name__ == "__main__":
    print(f"평가 하니스 진행 화면: http://127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
