"""端到端模拟测试（无需真实微信/钉钉/LLM）：

1. 启动完整应用（Mock 连接器 + Mock 引擎）
2. 模拟微信/钉钉收到新消息 → 验证自动生成回复
3. 验证人工审核（批准/驳回）
4. 验证直接对话接口

用法：
    cd backend && uv run python ../scripts/simulate.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from tests.conftest import make_settings  # noqa: E402


def p(label: str, msg: dict | None) -> None:
    if msg is None:
        return
    tag = {"wechat": "[微信]", "dingtalk": "[钉钉]", "web": "[网页]"}.get(msg["platform"], "?")
    arrow = "→" if msg["direction"] == "in" else "←"
    status = {"pending": "⏳待审核", "sent": "✅已发送", "blocked": "🚫已拦截", "rejected": "❌已驳回"}.get(
        msg.get("status"), ""
    )
    print(f"  {tag} {arrow} {msg['sender']}: {msg['content']}  {status}".encode("utf-8", "ignore").decode("utf-8", "ignore"))


def main() -> int:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    settings = make_settings()
    app = create_app(settings)

    passed, failed = 0, 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed, failed
        mark = "✅" if cond else "❌"
        cond and (passed := passed + 1)
        (not cond) and (failed := failed + 1)
        print(f"{mark} {name}" + (f"  ({detail})" if detail and not cond else ""))

    print("=" * 72)
    print("ChatAgent 端到端模拟测试")
    print("=" * 72)

    with TestClient(app, client=("127.0.0.1", 12345)) as client:  # 显式本机来源（白名单内）
        # 0. 健康检查 + 状态
        assert client.get("/api/health").json() == {"status": "ok"}
        st = client.get("/api/status").json()
        check("健康检查 /api/health", True)
        check("安全默认：连接器不自动启动", st["connectors"]["wechat"]["status"] == "stopped"
              and st["connectors"]["dingtalk"]["status"] == "stopped")
        check("发送模式默认人工确认", st.get("send_mode") == "manual_confirm")
        client.post("/api/connectors/wechat/start")
        client.post("/api/connectors/dingtalk/start")
        st = client.get("/api/status").json()
        check("按钮启动监听后运行中", st["connectors"]["wechat"]["status"] == "running"
              and st["connectors"]["dingtalk"]["status"] == "running")
        print(f"  引擎: {st['engine']} | 发送模式: {st.get('send_mode')}")

        # 1. 模拟微信/钉钉新消息 → 自动生成回复
        scenarios = [
            ("wechat", "wx-张三", "张三", "你好，在吗？"),
            ("wechat", "wx-张三", "张三", "明天下午3点的会议记得参加？"),
            ("dingtalk", "dt-项目群", "李四", "文档收到了，谢谢，辛苦了！"),
            ("dingtalk", "dt-项目群", "王五", "这个线上问题什么时候能修复？"),
        ]
        print("\n[场景1] 模拟收到新消息，验证自动触发生成回复")
        for platform, chat_id, sender, content in scenarios:
            r = client.post("/api/simulate", json={
                "platform": platform, "chat_id": chat_id, "sender": sender, "content": content,
            })
            data = r.json()
            p("收", data["message"])
            p("复", data["reply"])
            check(
                f"{platform}:{sender} 新消息触发回复",
                r.status_code == 200 and data["reply"] is not None
                and data["reply"]["status"] == "pending",
            )

        # 2. 人工审核：批准第一条，驳回第二条
        print("\n[场景2] 人工审核回复")
        pending = client.get("/api/replies", params={"status": "pending"}).json()["replies"]
        check("存在待审核回复", len(pending) >= 4, f"实际 {len(pending)} 条")
        r1 = client.post(f"/api/replies/{pending[0]['id']}/approve")
        check("批准回复 → 已发送", r1.status_code == 200 and r1.json()["message"]["status"] == "sent")
        r2 = client.post(f"/api/replies/{pending[1]['id']}/reject")
        check("驳回回复 → 已驳回", r2.status_code == 200 and r2.json()["message"]["status"] == "rejected")
        p("批", r1.json()["message"])

        # 3. 直接对话（原“对话测试”走的接口）
        print("\n[场景3] 直接对话 /api/chat")
        r = client.post("/api/chat", json={"content": "你好"})
        data = r.json()
        p("问", data["message"])
        p("答", data["reply"])
        check("直接对话返回回复", r.status_code == 200 and data["reply"]["direction"] == "out")

        # 4. 连接器控制
        print("\n[场景4] 连接器控制")
        r = client.post("/api/connectors/wechat/stop")
        check("停止微信连接器", r.json()["connector"]["status"] == "stopped")
        r = client.post("/api/connectors/wechat/start")
        check("重启微信连接器", r.json()["connector"]["status"] == "running")

        # 6. 消息总量校验
        total = client.get("/api/messages", params={"limit": 1000}).json()["messages"]
        ins = [m for m in total if m["direction"] == "in"]
        outs = [m for m in total if m["direction"] == "out"]
        check("每条新消息都有对应回复", len(outs) == len(ins), f"in={len(ins)} out={len(outs)}")

    print("=" * 72)
    print(f"结果: {passed} 通过, {failed} 失败")
    print("=" * 72)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
