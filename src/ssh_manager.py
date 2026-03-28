"""SSH client configuration and connection manager."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from src.config import DATA_DIR


SSH_CLIENTS_PATH = DATA_DIR / "ssh_clients.json"
_ACTIVE_CLIENTS: dict[str, Any] = {}
_ACTIVE_CLIENTS_LOCK = threading.RLock()


def _load_paramiko():
    try:
        import paramiko
    except ImportError as exc:
        raise RuntimeError("未安装 paramiko，无法使用 SSH 功能。") from exc
    return paramiko


def _default_clients_payload() -> dict[str, list[dict[str, Any]]]:
    return {"clients": []}


def load_clients_config() -> dict[str, list[dict[str, Any]]]:
    if not SSH_CLIENTS_PATH.exists():
        return _default_clients_payload()
    try:
        data = json.loads(SSH_CLIENTS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return _default_clients_payload()
    if not isinstance(data, dict):
        return _default_clients_payload()
    clients = data.get("clients")
    if not isinstance(clients, list):
        return _default_clients_payload()
    normalized: list[dict[str, Any]] = []
    for item in clients:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "name": str(item.get("name", "")).strip(),
                "host": str(item.get("host", "")).strip(),
                "port": int(item.get("port", 22) or 22),
                "username": str(item.get("username", "")).strip(),
                "auth_type": str(item.get("auth_type", "password")).strip() or "password",
                "private_key_path": str(item.get("private_key_path", "")).strip(),
                "password": str(item.get("password", "")),
            }
        )
    return {"clients": normalized}


def save_clients_config(data: dict[str, list[dict[str, Any]]]) -> None:
    SSH_CLIENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SSH_CLIENTS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def list_clients_config() -> list[dict[str, Any]]:
    return list(load_clients_config().get("clients", []))


def get_client_config(name: str) -> dict[str, Any] | None:
    target = name.strip()
    for item in list_clients_config():
        if item.get("name") == target:
            return item
    return None


def upsert_client_config(client: dict[str, Any]) -> None:
    data = load_clients_config()
    clients = data["clients"]
    target_name = str(client.get("name", "")).strip()
    replaced = False
    for index, item in enumerate(clients):
        if item.get("name") == target_name:
            clients[index] = client
            replaced = True
            break
    if not replaced:
        clients.append(client)
    clients.sort(key=lambda item: item.get("name", "").lower())
    save_clients_config(data)


def delete_client_config(name: str) -> None:
    target = name.strip()
    data = load_clients_config()
    data["clients"] = [item for item in data["clients"] if item.get("name") != target]
    save_clients_config(data)
    client_disconnect(target)


def _is_transport_alive(client: Any) -> bool:
    try:
        transport = client.get_transport()
        return bool(transport and transport.is_active())
    except Exception:
        return False


def client_list() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with _ACTIVE_CLIENTS_LOCK:
        active_names = {
            name for name, client in _ACTIVE_CLIENTS.items()
            if _is_transport_alive(client)
        }
    for item in list_clients_config():
        name = item.get("name", "")
        if not name:
            continue
        items.append(
            {
                "name": name,
                "connected": name in active_names,
            }
        )
    return items


def client_connect(name: str):
    config = get_client_config(name)
    if config is None:
        raise ValueError(f"未找到名为 `{name}` 的 SSH 服务器配置。")

    with _ACTIVE_CLIENTS_LOCK:
        existing = _ACTIVE_CLIENTS.get(name)
        if existing is not None and _is_transport_alive(existing):
            return existing, False
        if existing is not None:
            try:
                existing.close()
            except Exception:
                pass
            _ACTIVE_CLIENTS.pop(name, None)

    paramiko = _load_paramiko()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs: dict[str, Any] = {
        "hostname": config["host"],
        "port": int(config.get("port", 22) or 22),
        "username": config["username"],
        "timeout": 10,
        "banner_timeout": 10,
        "auth_timeout": 10,
    }
    auth_type = config.get("auth_type", "password")
    if auth_type == "private_key":
        key_path = str(config.get("private_key_path", "")).strip()
        if not key_path:
            raise ValueError(f"SSH 服务器 `{name}` 未配置私钥路径。")
        if not Path(key_path).expanduser().exists():
            raise FileNotFoundError(f"SSH 服务器 `{name}` 的私钥路径不存在：{key_path}")
        connect_kwargs["key_filename"] = str(Path(key_path).expanduser())
        connect_kwargs["look_for_keys"] = False
        connect_kwargs["allow_agent"] = False
    else:
        password = str(config.get("password", ""))
        if not password:
            raise ValueError(f"SSH 服务器 `{name}` 未配置密码。")
        connect_kwargs["password"] = password
        connect_kwargs["look_for_keys"] = False
        connect_kwargs["allow_agent"] = False

    client.connect(**connect_kwargs)
    with _ACTIVE_CLIENTS_LOCK:
        _ACTIVE_CLIENTS[name] = client
    return client, True


def client_command(name: str, command: str, timeout: int = 30) -> dict[str, Any]:
    with _ACTIVE_CLIENTS_LOCK:
        client = _ACTIVE_CLIENTS.get(name)
    if client is None:
        return {
            "ok": False,
            "error": f"SSH 会话 `{name}` 未连接，请先调用 client_connect。",
        }
    if not _is_transport_alive(client):
        client_disconnect(name)
        return {
            "ok": False,
            "error": f"SSH 会话 `{name}` 已断开，请重新调用 client_connect。",
        }

    stdin = stdout = stderr = None
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=max(1, int(timeout)))
        exit_code = stdout.channel.recv_exit_status()
        return {
            "ok": True,
            "stdout": stdout.read().decode("utf-8", errors="replace"),
            "stderr": stderr.read().decode("utf-8", errors="replace"),
            "exit_code": exit_code,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"远程命令执行失败：{exc}",
        }
    finally:
        for stream in (stdin, stdout, stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:
                pass


def client_disconnect(name: str) -> bool:
    with _ACTIVE_CLIENTS_LOCK:
        client = _ACTIVE_CLIENTS.pop(name, None)
    if client is None:
        return False
    try:
        client.close()
    except Exception:
        pass
    return True


def disconnect_all_clients() -> None:
    with _ACTIVE_CLIENTS_LOCK:
        names = list(_ACTIVE_CLIENTS.keys())
    for name in names:
        client_disconnect(name)
