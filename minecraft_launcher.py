#!/usr/bin/env python3
"""
Console launcher for managing one or more Minecraft servers on macOS.

The launcher keeps running servers as child Java processes while the launcher is
open, so it can forward console commands to them safely.
"""

from __future__ import annotations

import json
import os
import platform
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


APP_NAME = "Minecraft macOS Launcher"
ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / ".mc-launcher"
CONFIG_FILE = CONFIG_DIR / "config.json"
SERVERS_DIR = ROOT / "servers"
LIVE_LOG_DIR = CONFIG_DIR / "logs"
JAVA_DIR = CONFIG_DIR / "java"
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
CORE_LABELS = {
    "paper": "Paper",
    "purpur": "Purpur",
    "fabric": "Fabric",
    "forge": "Forge",
    "vanilla": "Vanilla",
    "custom": "Custom",
}
LOG_TAIL_LINES = 22


def clean_screen() -> None:
    os.system("clear")


def terminal_width() -> int:
    return max(72, min(shutil.get_terminal_size((96, 24)).columns, 120))


def hr(char: str = "-") -> str:
    return char * terminal_width()


def clip(value: Any, width: int) -> str:
    text = str(value)
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"


def print_header(title: str, subtitle: str | None = None) -> None:
    print(hr("="))
    print(title)
    if subtitle:
        print(subtitle)
    print(hr("="))


def pause() -> None:
    input("\nНажмите Enter, чтобы продолжить...")


def prompt(text: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default not in (None, "") else ""
    value = input(f"{text}{suffix}: ").strip()
    return value if value else (default or "")


def prompt_int(text: str, default: int, min_value: int | None = None) -> int:
    while True:
        raw = prompt(text, str(default))
        try:
            value = int(raw)
            if min_value is not None and value < min_value:
                print(f"Введите число не меньше {min_value}.")
                continue
            return value
        except ValueError:
            print("Введите целое число.")


def normalize_ram(value: str, default_unit: str = "G") -> str:
    raw = value.strip().upper().replace(" ", "")
    if not raw:
        raise ValueError("RAM не может быть пустым значением.")
    match = re.fullmatch(r"(\d+)([GM]?)", raw)
    if not match:
        raise ValueError("RAM указывается числом или числом с G/M, например 6G или 1024M.")
    number = int(match.group(1))
    if number <= 0:
        raise ValueError("RAM должна быть больше нуля.")
    unit = match.group(2) or default_unit
    return f"{number}{unit}"


def prompt_ram(text: str, default: str) -> str:
    while True:
        raw = prompt(text, default)
        try:
            return normalize_ram(raw)
        except ValueError as exc:
            print(exc)


def prompt_yes_no(text: str, default: bool = False) -> bool:
    marker = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{text} [{marker}]: ").strip().lower()
        if not raw:
            return default
        if raw in {"y", "yes", "д", "да"}:
            return True
        if raw in {"n", "no", "н", "нет"}:
            return False
        print("Ответьте y или n.")


def load_json_url(url: str) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def load_text_url(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.read().decode("utf-8").strip()


def download_file(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
    with urllib.request.urlopen(request, timeout=60) as response:
        total = int(response.headers.get("Content-Length") or 0)
        received = 0
        tmp = target.with_suffix(target.suffix + ".download")
        with tmp.open("wb") as file:
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                file.write(chunk)
                received += len(chunk)
                if total:
                    percent = int(received * 100 / total)
                    print(f"\rСкачивание: {percent:3d}%", end="", flush=True)
        if total:
            print()
        tmp.replace(target)


def default_config() -> dict[str, Any]:
    return {"version": 1, "servers": {}}


def load_config() -> dict[str, Any]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SERVERS_DIR.mkdir(parents=True, exist_ok=True)
    LIVE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    JAVA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        save_config(default_config())
    with CONFIG_FILE.open("r", encoding="utf-8") as file:
        data = json.load(file)
    data.setdefault("version", 1)
    data.setdefault("servers", {})
    return data


def save_config(config: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with CONFIG_FILE.open("w", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False, indent=2)
        file.write("\n")


def write_properties(path: Path, updates: dict[str, str]) -> None:
    values: dict[str, str] = {}
    order: list[str] = []

    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                order.append(line)
                continue
            key, value = stripped.split("=", 1)
            values[key] = value
            order.append(key)

    values.update(updates)
    if not order:
        order = ["motd", "server-port", "online-mode", "difficulty", "gamemode", "max-players"]

    written: set[str] = set()
    lines: list[str] = []
    for item in order:
        if item in values:
            lines.append(f"{item}={values[item]}")
            written.add(item)
        else:
            lines.append(item)

    for key, value in updates.items():
        if key not in written:
            lines.append(f"{key}={value}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def check_java(java_path: str = "java") -> str | None:
    executable = shutil.which(java_path) if java_path == "java" else java_path
    if not executable:
        return None
    try:
        result = subprocess.run(
            [executable, "-version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first_line = (result.stdout or "").splitlines()[0] if result.stdout else "Java найдена"
    return first_line


def adoptium_arch() -> str:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "aarch64"
    if machine in {"x86_64", "amd64"}:
        return "x64"
    raise RuntimeError(f"Архитектура macOS не поддержана для автоустановки Java: {machine}")


def recommended_java_major(minecraft_version: str) -> int:
    if minecraft_version.startswith("26."):
        return 25
    parts = minecraft_version.split(".")
    try:
        if len(parts) >= 2 and parts[0] == "1":
            minor = int(parts[1])
            patch = int(parts[2]) if len(parts) > 2 else 0
            if minor >= 21 or (minor == 20 and patch >= 5):
                return 21
            if minor >= 18 or (minor == 17 and patch >= 1):
                return 17
            if minor == 17:
                return 16
            if minor >= 12:
                return 11
    except ValueError:
        pass
    return 21


def find_java_binary(base: Path) -> Path | None:
    candidates = sorted(base.rglob("bin/java"))
    if not candidates:
        return None
    mac_home = [path for path in candidates if "Contents/Home/bin/java" in str(path)]
    return (mac_home or candidates)[0]


def safe_extract_tar(archive: Path, target: Path) -> None:
    target_resolved = target.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            member_path = (target / member.name).resolve()
            try:
                member_path.relative_to(target_resolved)
            except ValueError:
                raise RuntimeError("Архив Java содержит небезопасный путь.")
        tar.extractall(target)


def ensure_temurin_jdk(major: int) -> Path:
    install_dir = JAVA_DIR / f"temurin-{major}"
    existing = find_java_binary(install_dir)
    if existing and check_java(str(existing)):
        return existing
    if install_dir.exists():
        shutil.rmtree(install_dir)

    arch = adoptium_arch()
    url = (
        "https://api.adoptium.net/v3/binary/latest/"
        f"{major}/ga/mac/{arch}/jdk/hotspot/normal/eclipse?project=jdk"
    )
    archive = JAVA_DIR / f"temurin-{major}-mac-{arch}.tar.gz"
    extract_tmp = JAVA_DIR / f".extract-temurin-{major}"
    shutil.rmtree(extract_tmp, ignore_errors=True)
    extract_tmp.mkdir(parents=True, exist_ok=True)

    print(f"Скачиваю Eclipse Temurin JDK {major} для macOS {arch}...")
    download_file(url, archive)
    print("Распаковываю Java...")
    safe_extract_tar(archive, extract_tmp)
    extract_tmp.replace(install_dir)
    archive.unlink(missing_ok=True)

    java = find_java_binary(install_dir)
    if not java:
        raise RuntimeError("Java скачана, но bin/java не найден.")
    java.chmod(java.stat().st_mode | 0o111)
    status = check_java(str(java))
    if not status:
        raise RuntimeError("Скачанная Java не запускается.")
    print(f"Java готова: {status}")
    return java


def installed_temurin_javas() -> list[tuple[str, Path, str | None]]:
    result: list[tuple[str, Path, str | None]] = []
    if not JAVA_DIR.exists():
        return result
    for directory in sorted(JAVA_DIR.glob("temurin-*")):
        java = find_java_binary(directory)
        if java:
            result.append((directory.name, java, check_java(str(java))))
    return result


def server_dir(server: dict[str, Any]) -> Path:
    return ROOT / server["directory"]


def jar_path(server: dict[str, Any]) -> Path:
    return server_dir(server) / server.get("jar", "server.jar")


def launcher_log_path(name: str) -> Path:
    return LIVE_LOG_DIR / f"{name}.log"


def tail_text_file(path: Path, lines: int = LOG_TAIL_LINES) -> list[str]:
    if not path.exists():
        return []
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return []


def relative_to_server(server: dict[str, Any], path: Path) -> str:
    return str(path.relative_to(server_dir(server)))


def select_server(config: dict[str, Any], title: str = "Выберите сервер") -> str | None:
    names = sorted(config["servers"])
    if not names:
        print("Серверов пока нет.")
        return None
    print(title)
    for index, name in enumerate(names, 1):
        print(f"{index}. {name}")
    raw = prompt("Номер или имя")
    if raw in config["servers"]:
        return raw
    try:
        number = int(raw)
    except ValueError:
        print("Такого сервера нет.")
        return None
    if 1 <= number <= len(names):
        return names[number - 1]
    print("Такого сервера нет.")
    return None


def resolve_vanilla_download(version: str) -> tuple[str, str]:
    manifest = load_json_url("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
    if version == "latest":
        version = manifest["latest"]["release"]
    item = next((entry for entry in manifest["versions"] if entry["id"] == version), None)
    if not item:
        raise RuntimeError(f"Версия vanilla {version} не найдена.")
    details = load_json_url(item["url"])
    return version, details["downloads"]["server"]["url"]


def resolve_paper_download(version: str) -> tuple[str, str]:
    project = load_json_url("https://api.papermc.io/v2/projects/paper")
    versions = project["versions"]
    if version == "latest":
        version = versions[-1]
    if version not in versions:
        raise RuntimeError(f"Версия Paper {version} не найдена.")
    builds = load_json_url(f"https://api.papermc.io/v2/projects/paper/versions/{version}/builds")
    stable_builds = [build for build in builds["builds"] if build.get("channel") == "default"]
    build = (stable_builds or builds["builds"])[-1]["build"]
    file_name = f"paper-{version}-{build}.jar"
    url = f"https://api.papermc.io/v2/projects/paper/versions/{version}/builds/{build}/downloads/{file_name}"
    return version, url


def resolve_purpur_download(version: str) -> tuple[str, str]:
    project = load_json_url("https://api.purpurmc.org/v2/purpur")
    versions = project["versions"]
    if version == "latest":
        version = project.get("metadata", {}).get("current") or versions[-1]
    if version not in versions:
        raise RuntimeError(f"Версия Purpur {version} не найдена.")
    return version, f"https://api.purpurmc.org/v2/purpur/{version}/latest/download"


def latest_fabric_component(kind: str) -> str:
    items = load_json_url(f"https://meta.fabricmc.net/v2/versions/{kind}")
    stable = next((item for item in items if item.get("stable")), None)
    return (stable or items[0])["version"]


def resolve_fabric_download(version: str) -> tuple[str, str, dict[str, str]]:
    if version == "latest":
        version, _url = resolve_vanilla_download("latest")
    loader = latest_fabric_component("loader")
    installer = latest_fabric_component("installer")
    url = f"https://meta.fabricmc.net/v2/versions/loader/{version}/{loader}/{installer}/server/jar"
    meta = {"fabric_loader": loader, "fabric_installer": installer}
    return version, url, meta


def resolve_forge_installer(version: str, forge_build: str = "latest") -> tuple[str, str, str]:
    metadata = load_json_url("https://files.minecraftforge.net/net/minecraftforge/forge/maven-metadata.json")
    if version == "latest":
        version = list(metadata)[-1]
    builds = metadata.get(version)
    if not builds:
        raise RuntimeError(f"Версия Forge для Minecraft {version} не найдена.")
    if forge_build == "latest":
        full_version = builds[-1]
    else:
        full_version = forge_build if forge_build.startswith(f"{version}-") else f"{version}-{forge_build}"
        if full_version not in builds:
            raise RuntimeError(f"Forge build {full_version} не найден.")
    url = (
        "https://maven.minecraftforge.net/net/minecraftforge/forge/"
        f"{full_version}/forge-{full_version}-installer.jar"
    )
    return version, full_version, url


def discover_forge_launch(directory: Path, full_version: str) -> tuple[str, str]:
    args_files = sorted(directory.glob("libraries/net/minecraftforge/forge/*/unix_args.txt"))
    matching_args = [path for path in args_files if path.parent.name == full_version]
    if matching_args or args_files:
        path = (matching_args or args_files)[-1]
        return "argfile", str(path.relative_to(directory))

    jar_files = sorted(
        path
        for path in directory.glob("forge-*.jar")
        if "installer" not in path.name and "sources" not in path.name
    )
    matching_jars = [path for path in jar_files if full_version in path.name]
    if matching_jars or jar_files:
        path = (matching_jars or jar_files)[-1]
        return "jar", path.name

    raise RuntimeError("Forge установлен, но файл запуска не найден.")


def get_lan_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return socket.gethostbyname(socket.gethostname())
    finally:
        sock.close()


def get_public_ip() -> str:
    try:
        return load_text_url("https://api.ipify.org")
    except Exception:  # noqa: BLE001 - network diagnostics should stay friendly.
        return "не удалось определить"


@dataclass
class ManagedServer:
    name: str
    config: dict[str, Any]
    process: subprocess.Popen[str]
    output: deque[str] = field(default_factory=lambda: deque(maxlen=300))
    reader_thread: threading.Thread | None = None

    def is_running(self) -> bool:
        return self.process.poll() is None

    def send(self, command: str) -> None:
        if not self.is_running() or not self.process.stdin:
            raise RuntimeError("Сервер не запущен.")
        self.process.stdin.write(command.rstrip("\n") + "\n")
        self.process.stdin.flush()


class Launcher:
    def __init__(self) -> None:
        self.config = load_config()
        self.running: dict[str, ManagedServer] = {}
        signal.signal(signal.SIGINT, self._handle_sigint)

    def _handle_sigint(self, _signum: int, _frame: Any) -> None:
        print("\nПолучен Ctrl+C.")
        if self.running and prompt_yes_no("Остановить все запущенные серверы перед выходом?", True):
            self.stop_all()
        raise SystemExit(0)

    def menu(self) -> None:
        while True:
            clean_screen()
            print_header(APP_NAME, "Локальное управление Minecraft-серверами")
            self.print_servers()
            print(
                "\n"
                "1. Создать сервер\n"
                "2. Запустить сервер\n"
                "3. Остановить сервер\n"
                "4. Перезапустить сервер\n"
                "5. Команда в консоль сервера\n"
                "6. Логи и консоль сервера\n"
                "7. Настройки сервера\n"
                "8. Сеть и адрес подключения\n"
                "9. Показать Java\n"
                "10. Открыть папку сервера в Finder\n"
                "0. Выход"
            )
            choice = prompt("Действие")
            try:
                if choice == "1":
                    self.create_server()
                elif choice == "2":
                    self.start_selected()
                elif choice == "3":
                    self.stop_selected()
                elif choice == "4":
                    self.restart_selected()
                elif choice == "5":
                    self.command_selected()
                elif choice == "6":
                    self.console_selected()
                elif choice == "7":
                    self.configure_selected()
                elif choice == "8":
                    self.network_selected()
                elif choice == "9":
                    self.show_java()
                elif choice == "10":
                    self.open_finder()
                elif choice == "0":
                    self.exit()
                    return
                else:
                    print("Неизвестное действие.")
                    pause()
            except Exception as exc:  # noqa: BLE001 - keep the launcher alive for CLI users.
                print(f"\nОшибка: {exc}")
                pause()

    def print_servers(self) -> None:
        if not self.config["servers"]:
            print("Серверов пока нет. Создайте первый через пункт 1.")
            return
        print("Серверы")
        print(hr("-"))
        print(f"{'Имя':18} {'Статус':11} {'Ядро':8} {'Версия':10} {'RAM':11} {'Порт':5}")
        print(hr("-"))
        for name, server in sorted(self.config["servers"].items()):
            status = self.server_status(name)
            port = server.get("port", "?")
            kind = CORE_LABELS.get(server.get("type", "custom"), server.get("type", "custom"))
            version = server.get("minecraft_version", "unknown")
            ram = f"{normalize_ram(str(server.get('min_ram', '1G')))}-{normalize_ram(str(server.get('max_ram', '4G')))}"
            print(
                f"{clip(name, 18):18} "
                f"{status:11} "
                f"{clip(kind, 8):8} "
                f"{clip(version, 10):10} "
                f"{clip(ram, 11):11} "
                f"{port}"
            )
        print(hr("-"))

    def server_status(self, name: str) -> str:
        managed = self.running.get(name)
        if not managed:
            return "остановлен"
        if managed.is_running():
            return "запущен"
        code = managed.process.poll()
        return f"завершен:{code}"

    def create_server(self) -> None:
        clean_screen()
        print("Создание сервера\n")
        name = prompt("Имя сервера латиницей, например survival")
        if not name or not NAME_RE.match(name):
            print("Имя может содержать только латиницу, цифры, '.', '_' и '-'.")
            pause()
            return
        if name in self.config["servers"]:
            print("Сервер с таким именем уже есть.")
            pause()
            return

        print("\nТип ядра:")
        print("1. Paper (плагины, производительность)")
        print("2. Purpur (Paper + больше настроек)")
        print("3. Fabric (моды Fabric)")
        print("4. Forge (моды Forge)")
        print("5. Vanilla")
        print("6. Свой server.jar")
        kind_choice = prompt("Выбор", "1")
        server_type = {
            "1": "paper",
            "2": "purpur",
            "3": "fabric",
            "4": "forge",
            "5": "vanilla",
            "6": "custom",
        }.get(kind_choice, "paper")
        version = prompt("Версия Minecraft или latest", "latest")
        forge_build = "latest"
        if server_type == "forge":
            forge_build = prompt("Forge build или latest", "latest")
        print("\nJava:")
        print("1. Скачать автоматически Eclipse Temurin (рекомендуется)")
        print("2. Использовать системную java из PATH")
        print("3. Указать путь к java")
        java_choice = prompt("Выбор", "1")
        custom_java = ""
        if java_choice == "3":
            custom_java = prompt("Путь к bin/java").strip()
        min_ram = prompt_ram("Минимум RAM", "1G")
        max_ram = prompt_ram("Максимум RAM", "4G")
        port = prompt_int("Порт", 25565, 1)
        motd = prompt("MOTD", f"{name} Minecraft Server")
        online_mode = prompt_yes_no("Включить online-mode?", True)
        accept_eula = prompt_yes_no("Принять Minecraft EULA? https://aka.ms/MinecraftEULA", False)

        directory = SERVERS_DIR / name
        directory.mkdir(parents=True, exist_ok=False)
        jar = directory / "server.jar"
        resolved_version = version
        launch_mode = "jar"
        launch_target = "server.jar"
        install_meta: dict[str, Any] = {}
        java_path = "java"

        try:
            if server_type == "custom":
                source = Path(prompt("Путь к вашему .jar")).expanduser()
                if not source.exists():
                    raise RuntimeError("Указанный jar не найден.")
                shutil.copy2(source, jar)
                java_path = self.resolve_java_choice(version, java_choice, custom_java)
            elif server_type == "forge":
                resolved_version, forge_full_version, url = resolve_forge_installer(version, forge_build)
                java_path = self.resolve_java_choice(resolved_version, java_choice, custom_java)
                installer = directory / f"forge-{forge_full_version}-installer.jar"
                print(f"\nСкачиваю Forge installer {forge_full_version}...")
                download_file(url, installer)
                print("Устанавливаю Forge server...")
                result = subprocess.run(
                    [java_path, "-jar", installer.name, "--installServer"],
                    cwd=directory,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=300,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"Forge installer завершился с ошибкой:\n{result.stdout[-2000:]}")
                launch_mode, launch_target = discover_forge_launch(directory, forge_full_version)
                install_meta["forge_version"] = forge_full_version
                print(f"Forge установлен. Режим запуска: {launch_mode} {launch_target}")
            else:
                if server_type == "paper":
                    resolved_version, url = resolve_paper_download(version)
                elif server_type == "purpur":
                    resolved_version, url = resolve_purpur_download(version)
                elif server_type == "fabric":
                    resolved_version, url, install_meta = resolve_fabric_download(version)
                else:
                    resolved_version, url = resolve_vanilla_download(version)
                java_path = self.resolve_java_choice(resolved_version, java_choice, custom_java)
                print(f"\nСкачиваю {CORE_LABELS[server_type]} {resolved_version}...")
                download_file(url, jar)
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise

        (directory / "eula.txt").write_text(f"eula={'true' if accept_eula else 'false'}\n", encoding="utf-8")
        write_properties(
            directory / "server.properties",
            {
                "motd": motd,
                "server-port": str(port),
                "online-mode": "true" if online_mode else "false",
            },
        )

        self.config["servers"][name] = {
            "type": server_type,
            "minecraft_version": resolved_version,
            "directory": str(directory.relative_to(ROOT)),
            "jar": launch_target if launch_mode == "jar" else "server.jar",
            "launch_mode": launch_mode,
            "launch_target": launch_target,
            "java": java_path,
            "min_ram": min_ram,
            "max_ram": max_ram,
            "port": port,
            "extra_args": [],
            "nogui": True,
            "eula": accept_eula,
            **install_meta,
        }
        save_config(self.config)
        print(f"\nГотово: {directory}")
        if not accept_eula:
            print("Перед запуском примите EULA через настройки сервера или отредактируйте eula.txt.")
        elif prompt_yes_no("Запустить сервер сейчас?", True):
            self.start_server(name)
        pause()

    def resolve_java_choice(self, minecraft_version: str, choice: str, custom_java: str = "") -> str:
        if choice == "2":
            status = check_java("java")
            if not status:
                raise RuntimeError("Системная Java не найдена. Выберите автоустановку Java.")
            print(f"Использую системную Java: {status}")
            return "java"
        if choice == "3":
            java = str(Path(custom_java).expanduser())
            status = check_java(java)
            if not status:
                raise RuntimeError(f"Указанная Java не запускается: {java}")
            print(f"Использую Java: {status}")
            return java

        major = recommended_java_major(minecraft_version)
        java = ensure_temurin_jdk(major)
        return str(java)

    def start_selected(self) -> None:
        name = select_server(self.config, "Запуск сервера")
        if name:
            self.start_server(name)
        pause()

    def start_server(self, name: str) -> None:
        if name in self.running and self.running[name].is_running():
            print("Сервер уже запущен.")
            return
        server = self.config["servers"][name]
        directory = server_dir(server)
        self.validate_launch_target(server)
        if not (directory / "eula.txt").exists() or "eula=true" not in (directory / "eula.txt").read_text(
            encoding="utf-8", errors="replace"
        ):
            raise RuntimeError("EULA не принята. Откройте настройки сервера и примите EULA.")
        java = server.get("java", "java")
        java_status = check_java(java)
        if not java_status:
            major = recommended_java_major(server.get("minecraft_version", "latest"))
            print(f"Java не найдена. Для этого сервера рекомендована Java {major}.")
            if prompt_yes_no("Скачать Eclipse Temurin автоматически?", True):
                java = str(ensure_temurin_jdk(major))
                server["java"] = java
                save_config(self.config)
                java_status = check_java(java)
            else:
                raise RuntimeError("Java не найдена.")

        command = self.build_launch_command(server)

        live_log = launcher_log_path(name)
        log_file = live_log.open("a", encoding="utf-8")
        log_file.write(f"\n--- launch {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        log_file.flush()
        process = subprocess.Popen(
            command,
            cwd=directory,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        managed = ManagedServer(name=name, config=server, process=process)
        thread = threading.Thread(target=self._read_output, args=(managed, log_file), daemon=True)
        managed.reader_thread = thread
        self.running[name] = managed
        thread.start()
        print(f"Запущен {name} (pid {process.pid}). Java: {java_status}")
        print(f"Логи больше не выводятся поверх меню. Откройте пункт 6: Логи и консоль сервера.")

    def validate_launch_target(self, server: dict[str, Any]) -> None:
        directory = server_dir(server)
        launch_mode = server.get("launch_mode", "jar")
        launch_target = server.get("launch_target") or server.get("jar", "server.jar")
        if launch_mode == "argfile":
            target = directory / launch_target
            if not target.exists():
                raise RuntimeError(f"Не найден Forge args-файл: {target}")
            return
        jar = directory / launch_target
        if not jar.exists():
            raise RuntimeError(f"Не найден jar: {jar}")

    def build_launch_command(self, server: dict[str, Any]) -> list[str]:
        java = server.get("java", "java")
        command = [
            java,
            f"-Xms{normalize_ram(str(server.get('min_ram', '1G')))}",
            f"-Xmx{normalize_ram(str(server.get('max_ram', '4G')))}",
            *server.get("extra_args", []),
        ]
        launch_mode = server.get("launch_mode", "jar")
        launch_target = server.get("launch_target") or server.get("jar", "server.jar")
        if launch_mode == "argfile":
            command.append(f"@{launch_target}")
        else:
            command.extend(["-jar", launch_target])
        if server.get("nogui", True):
            command.append("nogui")
        return command

    def _read_output(self, managed: ManagedServer, log_file: Any) -> None:
        assert managed.process.stdout is not None
        try:
            for line in managed.process.stdout:
                line = line.rstrip("\n")
                managed.output.append(line)
                log_file.write(line + "\n")
                log_file.flush()
        finally:
            code = managed.process.poll()
            log_file.write(f"--- process exited: {code} ---\n")
            log_file.close()

    def stop_selected(self) -> None:
        name = select_server(self.config, "Остановка сервера")
        if name:
            self.stop_server(name)
        pause()

    def stop_server(self, name: str, timeout: int = 45) -> None:
        managed = self.running.get(name)
        if not managed or not managed.is_running():
            print("Сервер не запущен.")
            self.running.pop(name, None)
            return
        print(f"Останавливаю {name}...")
        try:
            managed.send("stop")
            managed.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            print("Сервер не завершился вовремя, принудительно завершаю процесс.")
            managed.process.terminate()
            try:
                managed.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                managed.process.kill()
        self.running.pop(name, None)
        print("Остановлен.")

    def restart_selected(self) -> None:
        name = select_server(self.config, "Перезапуск сервера")
        if name:
            self.stop_server(name)
            self.start_server(name)
        pause()

    def command_selected(self) -> None:
        name = select_server(self.config, "Команда в консоль сервера")
        if not name:
            pause()
            return
        command = prompt("Команда без слеша, например say Hello")
        if command:
            self.send_command(name, command)
        pause()

    def send_command(self, name: str, command: str) -> None:
        managed = self.running.get(name)
        if not managed or not managed.is_running():
            raise RuntimeError("Сервер не запущен.")
        managed.send(command)
        print("Команда отправлена.")

    def console_selected(self) -> None:
        name = select_server(self.config, "Логи и консоль сервера")
        if not name:
            pause()
            return
        while True:
            clean_screen()
            print_header(f"Логи и консоль: {name}", f"Статус: {self.server_status(name)}")
            self.print_recent_logs(name, LOG_TAIL_LINES)
            print(
                "\n"
                "1. Обновить последние строки\n"
                "2. Live-логи\n"
                "3. Отправить команду\n"
                "4. Открыть файл лога в Console/TextEdit\n"
                "0. Назад"
            )
            choice = prompt("Действие")
            if choice == "1":
                continue
            if choice == "2":
                self.live_logs(name)
            elif choice == "3":
                self.prompt_server_command(name)
            elif choice == "4":
                subprocess.run(["open", str(launcher_log_path(name))], check=False)
                pause()
            elif choice == "0":
                return
            else:
                print("Неизвестное действие.")
                pause()

    def get_recent_logs(self, name: str, lines: int = LOG_TAIL_LINES) -> list[str]:
        managed = self.running.get(name)
        if managed and managed.output:
            return list(managed.output)[-lines:]
        return tail_text_file(launcher_log_path(name), lines)

    def print_recent_logs(self, name: str, lines: int = LOG_TAIL_LINES) -> None:
        recent = self.get_recent_logs(name, lines)
        if not recent:
            print("Логов пока нет.")
            return
        width = terminal_width()
        for line in recent:
            print(clip(line, width))

    def live_logs(self, name: str) -> None:
        print("Live-логи: нажмите q затем Enter, чтобы вернуться.")
        time.sleep(0.7)
        while True:
            clean_screen()
            print_header(f"Live-логи: {name}", f"Статус: {self.server_status(name)} | q + Enter - назад")
            self.print_recent_logs(name, LOG_TAIL_LINES)
            readable, _writable, _error = select.select([sys.stdin], [], [], 1.0)
            if readable and sys.stdin.readline().strip().lower() == "q":
                return

    def prompt_server_command(self, name: str) -> None:
        managed = self.running.get(name)
        if not managed or not managed.is_running():
            print("Сервер не запущен.")
            pause()
            return
        print("\nКоманды вводятся без слеша. Примеры: say Привет, op Nick, whitelist on, stop")
        command = prompt(f"{name}>")
        if command:
            managed.send(command)
            print("Команда отправлена.")
        pause()

    def configure_selected(self) -> None:
        name = select_server(self.config, "Настройки сервера")
        if not name:
            pause()
            return
        server = self.config["servers"][name]
        directory = server_dir(server)
        while True:
            clean_screen()
            print(f"Настройки: {name}\n")
            print(f"1. Минимум RAM: {server.get('min_ram', '1G')}")
            print(f"2. Максимум RAM: {server.get('max_ram', '4G')}")
            print(f"3. Порт: {server.get('port', 25565)}")
            print(f"4. Java: {server.get('java', 'java')}")
            print(f"5. Доп. JVM args: {' '.join(server.get('extra_args', [])) or '-'}")
            print(f"6. Принять EULA: {server.get('eula', False)}")
            print("7. Изменить server.properties")
            print("8. Скачать/назначить Java")
            print("0. Назад")
            choice = prompt("Действие")
            if choice == "1":
                server["min_ram"] = prompt_ram("Минимум RAM", normalize_ram(str(server.get("min_ram", "1G"))))
            elif choice == "2":
                server["max_ram"] = prompt_ram("Максимум RAM", normalize_ram(str(server.get("max_ram", "4G"))))
            elif choice == "3":
                server["port"] = prompt_int("Порт", int(server.get("port", 25565)), 1)
                write_properties(directory / "server.properties", {"server-port": str(server["port"])})
            elif choice == "4":
                server["java"] = prompt("Путь к Java или java из PATH", server.get("java", "java"))
            elif choice == "5":
                raw = prompt("Аргументы через пробел", " ".join(server.get("extra_args", [])))
                server["extra_args"] = raw.split() if raw else []
            elif choice == "6":
                accepted = prompt_yes_no("Принять Minecraft EULA?", bool(server.get("eula", False)))
                server["eula"] = accepted
                (directory / "eula.txt").write_text(f"eula={'true' if accepted else 'false'}\n", encoding="utf-8")
            elif choice == "7":
                self.edit_properties(server)
            elif choice == "8":
                self.configure_java(server)
            elif choice == "0":
                save_config(self.config)
                return
            else:
                print("Неизвестное действие.")
                pause()
            save_config(self.config)

    def configure_java(self, server: dict[str, Any]) -> None:
        version = server.get("minecraft_version", "latest")
        default_major = recommended_java_major(version)
        print(f"\nMinecraft {version}: рекомендованная Java {default_major}.")
        print("1. Скачать и назначить Eclipse Temurin")
        print("2. Использовать системную java из PATH")
        print("3. Указать путь к java")
        choice = prompt("Выбор", "1")
        if choice == "1":
            major = prompt_int("Major version Java", default_major, 8)
            server["java"] = str(ensure_temurin_jdk(major))
        elif choice == "2":
            status = check_java("java")
            if not status:
                raise RuntimeError("Системная Java не найдена.")
            print(f"Системная Java: {status}")
            server["java"] = "java"
        elif choice == "3":
            java = str(Path(prompt("Путь к bin/java")).expanduser())
            status = check_java(java)
            if not status:
                raise RuntimeError("Указанная Java не запускается.")
            print(f"Java: {status}")
            server["java"] = java
        else:
            print("Неизвестное действие.")
            pause()
            return
        print(f"Назначено: {server['java']}")
        pause()

    def edit_properties(self, server: dict[str, Any]) -> None:
        path = server_dir(server) / "server.properties"
        print("\nОставьте значение пустым, чтобы не менять поле.")
        changes = {
            "motd": prompt("motd", ""),
            "max-players": prompt("max-players", ""),
            "difficulty": prompt("difficulty (peaceful/easy/normal/hard)", ""),
            "gamemode": prompt("gamemode (survival/creative/adventure/spectator)", ""),
            "online-mode": prompt("online-mode (true/false)", ""),
            "pvp": prompt("pvp (true/false)", ""),
            "view-distance": prompt("view-distance", ""),
        }
        updates = {key: value for key, value in changes.items() if value}
        if updates:
            write_properties(path, updates)
            print("server.properties обновлен.")
        else:
            print("Изменений нет.")
        pause()

    def network_selected(self) -> None:
        name = select_server(self.config, "Сеть и адрес подключения")
        if not name:
            pause()
            return
        server = self.config["servers"][name]
        lan_ip = get_lan_ip()
        public_ip = get_public_ip()
        port = int(server.get("port", 25565))
        clean_screen()
        print(f"Сеть: {name}\n")
        print(f"LAN-IP этого Mac:      {lan_ip}")
        print(f"Внешний IP:            {public_ip}")
        print(f"Порт Minecraft TCP:    {port}")
        print(f"Адрес в домашней сети: {lan_ip}:{port}")
        if public_ip != "не удалось определить":
            print(f"Адрес из интернета:    {public_ip}:{port}")
        print("\nДля белого IP обычно нужно:")
        print(f"1. В роутере пробросить TCP {port} -> {lan_ip}:{port}.")
        print("2. В macOS разрешить входящие подключения для Java, если Firewall спросит.")
        print("3. Оставить этот Mac включенным, пока сервер работает.")
        print("\nЕсли порт 25565, игроки часто могут писать только IP без :25565.")
        pause()

    def show_java(self) -> None:
        java = check_java()
        if java:
            print(f"Java в PATH: {java}")
        else:
            print("Java не найдена.")
        installed = installed_temurin_javas()
        if installed:
            print("\nJava, скачанная лаунчером:")
            for name, path, status in installed:
                print(f"- {name}: {status or 'не запускается'}")
                print(f"  {path}")
        else:
            print("\nЛаунчер пока не скачивал Java.")
        if prompt_yes_no("\nСкачать Java сейчас?", False):
            major = prompt_int("Major version Java", 21, 8)
            ensure_temurin_jdk(major)
        pause()

    def open_finder(self) -> None:
        name = select_server(self.config, "Открыть папку сервера")
        if not name:
            pause()
            return
        subprocess.run(["open", str(server_dir(self.config["servers"][name]))], check=False)
        pause()

    def stop_all(self) -> None:
        for name in list(self.running):
            self.stop_server(name)

    def exit(self) -> None:
        active = [name for name, managed in self.running.items() if managed.is_running()]
        if active:
            print(f"Запущены серверы: {', '.join(active)}")
            if prompt_yes_no("Остановить их перед выходом?", True):
                self.stop_all()
        print("Пока.")


def print_usage() -> None:
    print(
        "Использование:\n"
        "  ./minecraft_launcher.py              интерактивное меню\n"
        "  ./minecraft_launcher.py list         список серверов\n"
        "  ./minecraft_launcher.py cores        поддерживаемые ядра\n"
        "  ./minecraft_launcher.py java         проверка Java\n"
        "  ./minecraft_launcher.py java install 21|25\n"
        "\n"
        "Запуск и управление процессами выполняются из интерактивного меню,\n"
        "чтобы лаунчер мог держать stdin серверов открытым."
    )


def print_supported_cores() -> None:
    print("Поддерживаемые ядра для создания с нуля:")
    print("- Paper: плагины Bukkit/Spigot/Paper, прямое скачивание jar")
    print("- Purpur: форк Paper с расширенными настройками, прямое скачивание jar")
    print("- Fabric: моды Fabric, прямое скачивание server launcher jar")
    print("- Forge: моды Forge, скачивание installer и установка через --installServer")
    print("- Vanilla: официальный сервер Mojang")
    print("- Custom: ваш локальный server.jar")


def main(argv: list[str]) -> int:
    launcher = Launcher()
    if len(argv) == 1:
        launcher.menu()
        return 0
    command = argv[1]
    if command in {"-h", "--help", "help"}:
        print_usage()
        return 0
    if command == "list":
        launcher.print_servers()
        return 0
    if command == "cores":
        print_supported_cores()
        return 0
    if command == "java":
        if len(argv) >= 4 and argv[2] == "install":
            try:
                major = int(argv[3])
            except ValueError:
                print("Версия Java должна быть числом, например 21 или 25.")
                return 1
            java = ensure_temurin_jdk(major)
            print(java)
            return 0
        status = check_java()
        print(status or "Java не найдена.")
        installed = installed_temurin_javas()
        if installed:
            print("Скачанная лаунчером Java:")
            for name, path, java_status in installed:
                print(f"- {name}: {java_status or 'не запускается'}")
                print(f"  {path}")
        return 0 if status else 1
    print_usage()
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
