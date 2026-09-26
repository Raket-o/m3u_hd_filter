#!/usr/bin/env python3
"""
m3u_hd_filter.py — чистка M3U-плейлиста от HD-каналов.

Правила фильтрации:
  1. В названии канала есть «HD» (отдельным словом) — канал удаляется
     сразу, поток даже не проверяется.
  2. Иначе ffprobe подключается к потоку и читает высоту видеокадра.
     Высота >= --min-height (по умолчанию 720) — поток HD, канал удаляется.
  3. Если поток проверить не удалось — канал сохраняется
     (флаг --drop-on-error удаляет и такие).

Зависимости: Python 3.8+ и FFmpeg (утилита ffprobe должна быть в PATH).
Сторонние pip-пакеты не нужны — только стандартная библиотека.

Примеры запуска:
  python m3u_hd_filter.py playlist.m3u
  python m3u_hd_filter.py playlist.m3u -o clean.m3u --workers 8
  python m3u_hd_filter.py playlist.m3u --min-height 576 --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# «HD» отдельным словом: HD, (HD), - HD, HD Premium+ — подходят.
# FullHD и UHD сюда не попадают; если нужно, расширьте шаблон:
# r"(?<!\w)(hd|fhd|uhd)(?!\w)"
HD_NAME_RE = re.compile(r"(?<!\w)hd(?!\w)", re.IGNORECASE)


def is_hd_name(name: str) -> bool:
    """True, если в названии канала есть «HD»."""
    return HD_NAME_RE.search(name) is not None


def parse_m3u(text: str):
    """Возвращает (шапка #EXTM3U, список каналов {meta, url, name})."""
    lines = text.splitlines()
    header, i = [], 0
    if lines and lines[0].lstrip("\ufeff").startswith("#EXTM3U"):
        header.append(lines[0])
        i = 1

    channels = []
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("#EXTINF"):
            meta = [line]  # исходная строка #EXTINF со всеми атрибутами
            j = i + 1
            # директивы между #EXTINF и адресом (#EXTVLCOPT и т.п.)
            while j < len(lines) and lines[j].lstrip().startswith("#") \
                    and not lines[j].lstrip().startswith("#EXTINF"):
                meta.append(lines[j])
                j += 1
            url = lines[j].strip() if j < len(lines) else ""
            name = line.split(",", 1)[1].strip() if "," in line else ""
            channels.append({"meta": meta, "url": url, "name": name})
            i = j + 1
        else:
            i += 1
    return header, channels


def probe_height(url: str, timeout: int):
    """Высота видеопотока в пикселях или None, если проверить не удалось."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-probesize", "1000000",
        "-analyzeduration", "1000000",
        "-timeout", str(timeout * 1000000),  # сетевой таймаут, микросекунды
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "json",
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout + 15)
    except FileNotFoundError:
        sys.exit("Не найден ffprobe. Установите FFmpeg и добавьте его в PATH.")
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    try:
        streams = json.loads(proc.stdout)["streams"]
        return int(streams[0]["height"]) if streams else None
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Удаляет HD-каналы из M3U-плейлиста.")
    ap.add_argument("playlist", help="путь к исходному .m3u файлу")
    ap.add_argument("-o", "--output",
                    help="куда сохранить результат (по умолчанию <имя>_clean.m3u)")
    ap.add_argument("--min-height", type=int, default=720,
                    help="высота кадра, с которой поток считается HD (по умолчанию 720)")
    ap.add_argument("--timeout", type=int, default=10,
                    help="таймаут подключения к потоку, секунд (по умолчанию 10)")
    ap.add_argument("--workers", type=int, default=8,
                    help="сколько каналов проверять параллельно (по умолчанию 8)")
    ap.add_argument("--drop-on-error", action="store_true",
                    help="удалять каналы, поток которых не удалось проверить")
    ap.add_argument("--dry-run", action="store_true",
                    help="только показать статистику, файл не сохранять")
    args = ap.parse_args()

    if sys.platform == "win32":  # кириллица в консоли Windows
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    src = Path(args.playlist)
    if not src.is_file():
        sys.exit(f"Файл не найден: {src}")

    header, channels = parse_m3u(src.read_text(encoding="utf-8-sig",
                                               errors="replace"))
    total = len(channels)
    print(f"Плейлист: {src} · каналов найдено: {total}\n")

    removed = set()  # индексы удалённых каналов
    to_probe = []

    # Шаг 1. Мгновенный фильтр по названию — сеть не трогаем.
    for idx, ch in enumerate(channels):
        num = f"[{idx + 1}/{total}]"
        if is_hd_name(ch["name"]):
            print(f"{num} {ch['name']:<40} удалён · «HD» в названии")
            removed.add(idx)
        elif not ch["url"]:
            print(f"{num} {ch['name']:<40} без адреса · оставлен")
        else:
            to_probe.append(idx)

    # Шаг 2. Параллельная проверка фактического разрешения потока.
    def check(i):
        return i, probe_height(channels[i]["url"], args.timeout)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for i, height in pool.map(check, to_probe):
            ch = channels[i]
            num = f"[{i + 1}/{total}]"
            if height is None:
                if args.drop_on_error:
                    print(f"{num} {ch['name']:<40} поток недоступен · удалён")
                    removed.add(i)
                else:
                    print(f"{num} {ch['name']:<40} поток не проверен · оставлен")
            elif height >= args.min_height:
                print(f"{num} {ch['name']:<40} {height}p · удалён (HD-поток)")
                removed.add(i)
            else:
                print(f"{num} {ch['name']:<40} {height}p · оставлен")

    # Шаг 3. Сборка плейлиста в исходном порядке, атрибуты без изменений.
    kept = [ch for i, ch in enumerate(channels) if i not in removed]
    out_lines = list(header)
    for ch in kept:
        out_lines.extend(ch["meta"])
        if ch["url"]:
            out_lines.append(ch["url"])

    print(f"\nИтог: оставлено {len(kept)}, удалено {len(removed)}.")
    if args.dry_run:
        print("Режим --dry-run: файл не сохранён.")
        return

    out_path = Path(args.output) if args.output else src.with_name(
        f"{src.stem}_clean{src.suffix or '.m3u'}")
    out_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    print(f"Сохранено: {out_path}")


if __name__ == "__main__":
    main()