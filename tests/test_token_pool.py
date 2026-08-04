"""账号池与 Firefly 调用租约的本地测试，不访问外网。"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import firefly_pipeline as fp
from db import Database
from token_pool import TokenPool


def test_pool_release_and_refresh_policy() -> None:
    with TemporaryDirectory() as tmp:
        pool = TokenPool(Database(Path(tmp) / "test.db"))
        account = pool.add(
            token="token-a",
            label="a",
            cookies=[{"name": "ims_sid", "value": "x"}],
            expires_at=time.time() + 3600,
        )

        acquired, release = pool.acquire(timeout=0)
        assert acquired.id == account.id
        assert account.in_use == 1
        release(True)
        release(True)  # release 必须幂等
        assert account.in_use == 0
        assert account.total_succeeded == 1

        # 模型发现、额度读取等元数据请求只归还账号，不计入生成成功/失败。
        _metadata_account, release_metadata = pool.acquire(timeout=0)
        release_metadata(True, record_stats=False)
        assert account.in_use == 0
        assert account.total_succeeded == 1

        _metadata_account, release_metadata_failure = pool.acquire(timeout=0)
        release_metadata_failure(False, "credits HTTP 401", record_stats=False)
        assert account.total_failed == 0
        assert account.cooldown_until > time.time()
        account.cooldown_until = 0.0

        account.cooldown_until = time.time() + 60
        account.last_error = "quota exhausted"
        assert not pool.should_auto_refresh(account)
        account.last_error = "HTTP 401 unauthorized"
        assert pool.should_auto_refresh(account)


def test_firefly_lease_released_on_success_and_error() -> None:
    calls: list[tuple[bool, str]] = []
    original = fp.release_token
    fp.release_token = lambda ok, error="": calls.append((ok, error))  # type: ignore[assignment]
    try:
        @fp._release_firefly_call
        def success() -> int:
            return 1

        @fp._release_firefly_call
        def failure() -> None:
            raise RuntimeError("network failed")

        assert success() == 1
        try:
            failure()
        except RuntimeError:
            pass
        else:
            raise AssertionError("failure() should raise")
        assert calls[0] == (True, "")
        assert calls[1][0] is False and "network failed" in calls[1][1]
    finally:
        fp.release_token = original  # type: ignore[assignment]


def test_upstream_error_details_are_safe_and_actionable() -> None:
    error = fp.UpstreamResponseError(
        422,
        "query",
        {"message": "model does not support this request", "token": "secret-value"},
    )
    details = fp.upstream_error_details(error)
    assert details["http_status"] == 422
    assert details["stage"] == "query"
    assert details["detail"] == "model does not support this request"
    assert "secret-value" not in str(error)
    assert "HTTP 422" in fp.summarize_upstream_error(error)
    assert "TLS" in fp.summarize_upstream_error("SSLError: TLS connect error")
    assert "模型权限" in fp.summarize_upstream_error("HTTP 403 forbidden")


def main() -> int:
    test_pool_release_and_refresh_policy()
    test_firefly_lease_released_on_success_and_error()
    test_upstream_error_details_are_safe_and_actionable()
    print("=== token pool tests passed ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
