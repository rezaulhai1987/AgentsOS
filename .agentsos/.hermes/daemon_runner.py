import asyncio, json, sys
from pathlib import Path
from agentsos.daemon import Daemon, DaemonConfig
from agentsos.telegram.bridge import attach_bridge
from agentsos.work_registry import Registry

async def control_loop(d, sd):
    while not d._stop.is_set():
        await asyncio.sleep(0.5)
        cp = sd / "control.json"
        if not cp.exists():
            continue
        try:
            payload = json.loads(cp.read_text(encoding="utf-8"))
            cp.unlink()
        except Exception:
            continue
        cmd, reason = payload.get("cmd"), str(payload.get("reason", "control-file"))
        if cmd == "pause":
            await d.pause(reason=reason)
        elif cmd == "resume":
            await d.resume(reason=reason)
        elif cmd == "shutdown":
            await d.shutdown(reason=reason)
            return

async def main():
    sd = Path(sys.argv[1])
    ceiling = float(sys.argv[2])
    # v0.3.10: shared Registry factory for /goal on Telegram +
    # `agents goal ...` CLI. Same file = same view.
    reg_path = sd / "work_registry.json"

    def reg_factory():
        return Registry(path=reg_path)

    cfg = DaemonConfig(state_dir=sd, daily_ceiling_usd=ceiling,
                       extra_tasks=[attach_bridge(registry_factory=reg_factory)])
    d = Daemon(cfg)
    await d.start()
    await asyncio.gather(d.wait(), control_loop(d, sd))

if __name__ == "__main__":
    asyncio.run(main())
