"""把「阿凌」人设写进 AstrBot 的 SQLite（personas 表），可选同时写插件配置。

适用场景：你不想在 WebUI 里手动粘贴角色卡，或者想用脚本做可重复部署。

用法（在仓库根目录执行）：

    # 1) 先看会写入什么（不动数据库）
    python3 scripts/bootstrap_aling.py --db astrbot/data/astrbot/data_v4.db --dry-run

    # 2) 真正写入（建议先 docker compose stop astrbot，避免和运行中的实例抢写）
    python3 scripts/bootstrap_aling.py --db astrbot/data/astrbot/data_v4.db

    # 3) 顺便把插件配置按仓库里的示例覆盖过去
    python3 scripts/bootstrap_aling.py --db astrbot/data/astrbot/data_v4.db --sync-plugin-config

人设正文取自 `astrbot/persona_aling.txt`；插件配置取自
`astrbot/data/astrbot/config/astrbot_plugin_aling_memory_config.json`。

数据库表结构（AstrBot 4.29.x）：personas(persona_id, system_prompt, begin_dialogs,
tools, skills, ...)，persona_id 唯一。脚本是“存在就更新、不存在就插入”，幂等。

退出码：0 成功；1 文件/表不存在等技术错误。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PERSONA = ROOT / "astrbot" / "persona_aling.txt"
DEFAULT_PLUGIN_CFG = (
    ROOT / "astrbot" / "data" / "astrbot" / "config" / "astrbot_plugin_aling_memory_config.json"
)


def ensure_persona(db: Path, persona_id: str, prompt: str, dry_run: bool) -> None:
    con = sqlite3.connect(db)
    try:
        tables = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        if "personas" not in tables:
            raise SystemExit(f"[bootstrap] {db} 里没有 personas 表，先启动一次 AstrBot 让它建库。")
        row = con.execute(
            "select id from personas where persona_id=?", (persona_id,)
        ).fetchone()
        if dry_run:
            print(f"[dry-run] persona '{persona_id}' "
                  f"{'已存在，将更新' if row else '不存在，将插入'}，正文 {len(prompt)} 字")
            return
        if row:
            con.execute(
                "update personas set system_prompt=?, updated_at=datetime('now') where persona_id=?",
                (prompt, persona_id),
            )
            action = "updated"
        else:
            con.execute(
                "insert into personas (created_at, updated_at, persona_id, system_prompt,"
                " begin_dialogs, tools, skills, sort_order) values"
                " (datetime('now'), datetime('now'), ?, ?, '[]', '[]', '[]', 0)",
                (persona_id, prompt),
            )
            action = "inserted"
        con.commit()
        n = con.execute("select count(*) from personas").fetchone()[0]
        print(f"[bootstrap] persona '{persona_id}' {action}，正文 {len(prompt)} 字；表内共 {n} 条")
    finally:
        con.close()


def sync_plugin_config(src: Path, dst: Path, dry_run: bool) -> None:
    if not src.exists():
        print(f"[bootstrap] 跳过插件配置：{src} 不存在", file=sys.stderr)
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(src.read_text(encoding="utf-8-sig"))
    if dry_run:
        print(f"[dry-run] 会用 {src.name} 覆盖 {dst}")
        return
    dst.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
    print(f"[bootstrap] 已写入插件配置 {dst}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="AstrBot 的 data_v4.db 路径")
    ap.add_argument("--persona-id", default="aling")
    ap.add_argument("--persona-file", default=str(DEFAULT_PERSONA))
    ap.add_argument("--sync-plugin-config", action="store_true")
    ap.add_argument("--plugin-config", default=str(DEFAULT_PLUGIN_CFG))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    db = Path(args.db)
    persona_file = Path(args.persona_file)
    if not db.exists():
        print(f"[bootstrap] 数据库不存在：{db}", file=sys.stderr)
        return 1
    if not persona_file.exists():
        print(f"[bootstrap] 人设文件不存在：{persona_file}", file=sys.stderr)
        return 1
    ensure_persona(db, args.persona_id, persona_file.read_text(encoding="utf-8").strip(), args.dry_run)
    if args.sync_plugin_config:
        sync_plugin_config(Path(args.plugin_config), db.parent / "config" /
                           "astrbot_plugin_aling_memory_config.json", args.dry_run)
    print("[bootstrap] 完成。默认人格请在 AstrBot 配置里确认 "
          "agent_runner.config.persona.persona_id = 'aling'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
