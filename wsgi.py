"""Gunicorn entrypoint."""
import os

from app import _backfill_model_capabilities_from_logs, _recover_orphaned_jobs, app

# Gunicorn 直接导入 wsgi:app，不会执行 app.py 的 main()。
# 因此必须在生产 worker 加载时恢复上次未完成的任务。
_recover_orphaned_jobs()
_backfill_model_capabilities_from_logs()

if __name__ == "__main__":
    # Local debugging only; production runs gunicorn directly via systemd.
    port = int(os.environ.get("FLASK_PORT", "19998"))
    host = os.environ.get("FLASK_HOST", "127.0.0.1")
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
