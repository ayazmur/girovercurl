#!/usr/bin/env python3
"""
GitShell - Неубиваемая оболочка для Git через pycurl
С поддержкой цветов в Windows
"""

import os
import sys
import json
import subprocess
import shutil
import re
import base64
import io
from pathlib import Path
from typing import Optional, List, Dict, Tuple
import traceback

# ============================================================
# ВКЛЮЧАЕМ ЦВЕТА В WINDOWS
# ============================================================

if sys.platform == 'win32':
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # Включаем виртуальный терминал для ANSI-цветов
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        mode.value |= 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        kernel32.SetConsoleMode(handle, mode)
    except:
        pass


# ============================================================
# ЦВЕТА
# ============================================================

class Colors:
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BOLD = '\033[1m'
    DIM = '\033[2m'
    END = '\033[0m'

    @staticmethod
    def strip(text):
        """Удаляет ANSI-коды из текста (для логов)"""
        import re
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return ansi_escape.sub('', text)


# ============================================================
# БЕЗОПАСНЫЙ ИМПОРТ МОДУЛЕЙ
# ============================================================

READLINE_AVAILABLE = False
try:
    import readline

    READLINE_AVAILABLE = True
except ImportError:
    try:
        import pyreadline3 as readline

        READLINE_AVAILABLE = True
    except:
        pass

PYCURL_AVAILABLE = False
try:
    import pycurl
    import certifi

    PYCURL_AVAILABLE = True
except:
    pass

REGISTRY_AVAILABLE = False
try:
    import winreg

    REGISTRY_AVAILABLE = True
except:
    pass


# ============================================================
# РАБОТА С РЕЕСТРОМ
# ============================================================

def save_token_to_registry(token: str) -> bool:
    if not REGISTRY_AVAILABLE:
        return False
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"SOFTWARE\GitShell")
        winreg.SetValueEx(key, "GitHubToken", 0, winreg.REG_SZ, token)
        winreg.CloseKey(key)
        return True
    except:
        return False


def load_token_from_registry() -> Optional[str]:
    if not REGISTRY_AVAILABLE:
        return None
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"SOFTWARE\GitShell", 0, winreg.KEY_READ)
        token, _ = winreg.QueryValueEx(key, "GitHubToken")
        winreg.CloseKey(key)
        return token
    except:
        return None


def delete_token_from_registry() -> bool:
    if not REGISTRY_AVAILABLE:
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"SOFTWARE\GitShell", 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(key, "GitHubToken")
        winreg.CloseKey(key)
        return True
    except:
        return False


# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================

CONFIG_FILE = Path.home() / ".gitshell-config.json"
HISTORY_FILE = Path.home() / ".gitshell-history"


# ============================================================
# GIT OVER PYCURL
# ============================================================

class GitOverPyCurl:
    def __init__(self, repo_path: str, github_token: str):
        self.repo_path = Path(repo_path)
        self.github_token = github_token
        self.api_base = "https://api.github.com"
        self.proxy_url = "http://rnt-proxy.rn-t.ru"
        self.proxy_port = 3128
        self.owner = "unknown"
        self.repo_name = "unknown"
        self._parse_remote_url()

    def _parse_remote_url(self):
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=10
            )
            url = result.stdout.strip()
            if "github.com" in url:
                if "://" in url:
                    match = re.search(r"github\.com[:/](.+?)(?:\.git)?$", url)
                else:
                    match = re.search(r"github\.com:(.+?)(?:\.git)?$", url)
                if match:
                    parts = match.group(1).split('/')
                    if len(parts) >= 2:
                        self.owner = parts[0]
                        self.repo_name = parts[1]
        except:
            pass

    def get_current_branch(self) -> str:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self.repo_path,
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except:
            pass
        return "main"

    def _make_request(self, method: str, endpoint: str, data: Optional[Dict] = None) -> Dict:
        if not PYCURL_AVAILABLE:
            raise Exception("pycurl не доступен")

        url = f"{self.api_base}{endpoint}"
        buf = io.BytesIO()
        c = pycurl.Curl()

        try:
            c.setopt(c.URL, url)
            c.setopt(c.PROXY, self.proxy_url)
            c.setopt(c.PROXYPORT, self.proxy_port)

            # ===== АВТОРИЗАЦИЯ НА ПРОКСИ =====
            # Если прокси требует логин/пароль — укажите здесь
            # Формат: "username:password"
            c.setopt(c.PROXYUSERPWD, ":")  # Пустые логин и пароль
            c.setopt(c.PROXYAUTH, pycurl.HTTPAUTH_ANY)
            c.setopt(c.HTTPPROXYTUNNEL, 1)

            c.setopt(c.SSL_VERIFYPEER, 0)
            c.setopt(c.SSL_VERIFYHOST, 0)

            headers = [
                "Accept: application/vnd.github.v3+json",
                "Content-Type: application/json",
                "User-Agent: GitShell/1.0"
            ]
            if self.github_token:
                headers.append(f"Authorization: token {self.github_token}")

            c.setopt(c.HTTPHEADER, headers)

            method = method.upper()
            if method == "GET":
                c.setopt(c.HTTPGET, 1)
            elif method == "POST":
                c.setopt(c.POST, 1)
                if data:
                    c.setopt(c.POSTFIELDS, json.dumps(data))
            elif method == "PUT":
                c.setopt(c.CUSTOMREQUEST, "PUT")
                if data:
                    c.setopt(c.POSTFIELDS, json.dumps(data))
            elif method == "PATCH":
                c.setopt(c.CUSTOMREQUEST, "PATCH")
                if data:
                    c.setopt(c.POSTFIELDS, json.dumps(data))

            c.setopt(c.WRITEDATA, buf)
            c.setopt(c.TIMEOUT, 120)  # Увеличил таймаут
            c.setopt(c.CONNECTTIMEOUT, 60)

            c.perform()

            http_code = c.getinfo(c.RESPONSE_CODE)
            response = buf.getvalue().decode("utf-8")

            if http_code >= 400:
                raise Exception(f"API Error {http_code}: {response[:200]}")

            return json.loads(response) if response else {}
        except pycurl.error as e:
            # Расшифровка ошибок pycurl
            error_code, error_msg = e.args
            raise Exception(f"PycURL error {error_code}: {error_msg}")
        except Exception as e:
            raise Exception(f"Request failed: {e}")
        finally:
            try:
                c.close()
            except:
                pass

    def push_files(self, files: List[Tuple[str, str]], commit_message: str, branch: Optional[str] = None) -> bool:
        try:
            if self.owner == "unknown":
                raise Exception("Не удалось определить репозиторий")

            branch = branch or self.get_current_branch()

            ref_info = self._make_request("GET", f"/repos/{self.owner}/{self.repo_name}/git/refs/heads/{branch}")
            latest_commit_sha = ref_info["object"]["sha"]

            commit_info = self._make_request("GET", f"/repos/{self.owner}/{self.repo_name}/commits/{latest_commit_sha}")
            tree_sha = commit_info["commit"]["tree"]["sha"]
            tree_info = self._make_request("GET",
                                           f"/repos/{self.owner}/{self.repo_name}/git/trees/{tree_sha}?recursive=1")

            file_map = {}
            for file_path, content in files:
                encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
                blob = self._make_request(
                    "POST",
                    f"/repos/{self.owner}/{self.repo_name}/git/blobs",
                    data={"content": encoded, "encoding": "base64"}
                )
                file_map[file_path] = blob["sha"]

            tree_items = []
            existing = {item["path"]: item for item in tree_info.get("tree", [])}

            for file_path, blob_sha in file_map.items():
                if file_path in existing:
                    tree_items.append({
                        "path": file_path,
                        "mode": existing[file_path].get("mode", "100644"),
                        "type": "blob",
                        "sha": blob_sha
                    })
                else:
                    tree_items.append({
                        "path": file_path,
                        "mode": "100644",
                        "type": "blob",
                        "sha": blob_sha
                    })

            for path, item in existing.items():
                if path not in file_map:
                    tree_items.append({
                        "path": path,
                        "mode": item.get("mode", "100644"),
                        "type": item["type"],
                        "sha": item["sha"]
                    })

            new_tree = self._make_request(
                "POST",
                f"/repos/{self.owner}/{self.repo_name}/git/trees",
                data={"tree": tree_items, "base_tree": tree_sha}
            )

            new_commit = self._make_request(
                "POST",
                f"/repos/{self.owner}/{self.repo_name}/git/commits",
                data={"message": commit_message, "tree": new_tree["sha"], "parents": [latest_commit_sha]}
            )

            self._make_request(
                "PATCH",
                f"/repos/{self.owner}/{self.repo_name}/git/refs/heads/{branch}",
                data={"sha": new_commit["sha"], "force": False}
            )

            return True
        except Exception as e:
            raise Exception(f"Push failed: {e}")


# ============================================================
# ОСНОВНОЙ КЛАСС GitShell
# ============================================================

class GitShell:
    def __init__(self):
        self.current_path = Path.cwd()
        self.token = None
        self.git_client = None
        self.alias = {}
        self.running = True

        self._safe_load_token()
        self._safe_load_config()
        self._safe_setup_history()

    def _safe_load_token(self):
        try:
            token = load_token_from_registry()
            if token:
                self.token = token
                return
        except:
            pass

        try:
            if CONFIG_FILE.exists():
                config = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                token = config.get("github_token")
                if token:
                    self.token = token
                    save_token_to_registry(token)
                    return
        except:
            pass

    def _safe_load_config(self):
        try:
            if CONFIG_FILE.exists():
                config = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
                self.alias = config.get("alias", {})
        except:
            pass

    def _safe_setup_history(self):
        try:
            if READLINE_AVAILABLE:
                readline.set_history_length(1000)
                if HISTORY_FILE.exists():
                    readline.read_history_file(HISTORY_FILE)
        except:
            pass

    def _safe_save_history(self):
        try:
            if READLINE_AVAILABLE:
                readline.write_history_file(HISTORY_FILE)
        except:
            pass

    def _safe_get_input(self, prompt: str) -> str:
        try:
            return input(prompt)
        except KeyboardInterrupt:
            raise
        except:
            return ""

    def _safe_run_git(self, args: List[str]) -> Tuple[int, str, str]:
        try:
            result = subprocess.run(
                ["git"] + args,
                cwd=self.current_path,
                capture_output=True,
                text=True,
                timeout=30
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return 1, "", "Timeout"
        except Exception as e:
            return 1, "", str(e)

    def _safe_get_branch(self) -> str:
        try:
            if (self.current_path / ".git").exists():
                result = subprocess.run(
                    ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                    cwd=self.current_path,
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0:
                    return result.stdout.strip()
        except:
            pass
        return ""

    def _safe_get_status(self) -> bool:
        try:
            if (self.current_path / ".git").exists():
                result = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=self.current_path,
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                return bool(result.stdout.strip())
        except:
            pass
        return False

    def _safe_get_prompt(self) -> str:
        try:
            branch = self._safe_get_branch()
            if branch:
                branch_part = f"{Colors.GREEN}{branch}{Colors.END}"
            else:
                branch_part = f"{Colors.BLUE}no-repo{Colors.END}"

            path = str(self.current_path)
            home = str(Path.home())
            if path.startswith(home):
                path = "~" + path[len(home):]

            if len(path) > 40:
                parts = path.split(os.sep)
                if len(parts) > 3:
                    path = os.sep.join(["..." + parts[0][:2], *parts[-3:]])

            changes = f"{Colors.YELLOW}*{Colors.END}" if self._safe_get_status() else ""

            return f"{Colors.CYAN}❯ {Colors.BOLD}{branch_part}{Colors.END} {Colors.DIM}{path}{Colors.END}{changes} {Colors.CYAN}${Colors.END} "
        except:
            return f"{Colors.CYAN}❯ error {Colors.CYAN}${Colors.END} "

    def _cmd_cd(self, args):
        try:
            if len(args) > 1:
                new_path = Path(args[1])
                if not new_path.is_absolute():
                    new_path = self.current_path / new_path
                if new_path.exists() and new_path.is_dir():
                    self.current_path = new_path.resolve()
                    self.git_client = None
                else:
                    print(f"{Colors.RED}❌ Папка не найдена{Colors.END}")
            else:
                self.current_path = Path.home()
                self.git_client = None
        except Exception as e:
            print(f"{Colors.RED}❌ Ошибка cd: {e}{Colors.END}")

    def _cmd_pwd(self, args):
        try:
            print(self.current_path)
        except Exception as e:
            print(f"{Colors.RED}❌ Ошибка: {e}{Colors.END}")

    def _cmd_ls(self, args):
        try:
            items = sorted(self.current_path.iterdir())
            for item in items:
                if item.is_dir():
                    print(f"{Colors.BLUE}{item.name}/{Colors.END}")
                else:
                    print(item.name)
        except Exception as e:
            print(f"{Colors.RED}❌ Ошибка ls: {e}{Colors.END}")

    def _cmd_config(self, args):
        try:
            if len(args) > 1:
                if args[1] == "token":
                    if len(args) > 2:
                        self.token = args[2]
                        save_token_to_registry(self.token)
                        self.git_client = None
                        print(f"{Colors.GREEN}✅ Токен сохранен в реестр{Colors.END}")
                    else:
                        print(f"🔑 Токен: {self.token[:10] + '...' if self.token else 'Не установлен'}")
                elif args[1] == "delete-token":
                    if delete_token_from_registry():
                        self.token = None
                        print(f"{Colors.GREEN}✅ Токен удален{Colors.END}")
                    else:
                        print(f"{Colors.RED}❌ Ошибка удаления{Colors.END}")
                else:
                    print(
                        f"{Colors.RED}❌ Неизвестная опция. Используйте: config token <токен> | config delete-token{Colors.END}")
            else:
                print(f"{Colors.YELLOW}📋 Конфигурация:{Colors.END}")
                print(f"  Токен: {self.token[:10] + '...' if self.token else 'Не установлен'}")
                print(f"  Токен сохранен в: реестр Windows (HKCU\\SOFTWARE\\GitShell)")
                print(f"  Git: {'✅' if shutil.which('git') else '❌'}")
                print(f"  PyCurl: {'✅' if PYCURL_AVAILABLE else '❌'}")
                print(f"  Автодополнение: {'✅' if READLINE_AVAILABLE else '❌'}")
        except Exception as e:
            print(f"{Colors.RED}❌ Ошибка config: {e}{Colors.END}")

    def _cmd_push(self, args):
        try:
            if not self.token:
                print(f"{Colors.RED}❌ Токен не настроен. Используйте: config token <токен>{Colors.END}")
                return

            if not PYCURL_AVAILABLE:
                print(f"{Colors.RED}❌ PyCurl не доступен{Colors.END}")
                return

            if not (self.current_path / ".git").exists():
                print(f"{Colors.RED}❌ Не в Git репозитории{Colors.END}")
                return

            # Определяем ветку
            branch = args[1] if len(args) > 1 else self._safe_get_branch()
            if not branch:
                branch = "main"

            # === 1. Проверяем незакоммиченные изменения ===
            status_result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.current_path,
                capture_output=True,
                text=True,
                timeout=10
            )

            changed_files = []
            for line in status_result.stdout.strip().split("\n"):
                if line:
                    parts = line.split()
                    if len(parts) >= 2 and not line.startswith("??"):
                        changed_files.append(parts[1])

            if changed_files:
                print(f"{Colors.YELLOW}⚠️ Есть незакоммиченные изменения:{Colors.END}")
                for f in changed_files[:5]:
                    print(f"  {f}")
                if len(changed_files) > 5:
                    print(f"  ... и еще {len(changed_files) - 5} файлов")

                answer = input("Закоммитить их перед пушем? (y/n): ").strip().lower()
                if answer == 'y':
                    msg = input("Сообщение коммита: ").strip()
                    if not msg:
                        msg = "Update via GitShell"
                    subprocess.run(["git", "add", "."], cwd=self.current_path, capture_output=True)
                    subprocess.run(["git", "commit", "-m", msg], cwd=self.current_path, capture_output=True)
                    print(f"{Colors.GREEN}✅ Коммит создан{Colors.END}")
                else:
                    print("ℹ️ Пуш отменен")
                    return

            # === 2. Получаем последний коммит ===
            commit_hash = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=self.current_path,
                capture_output=True,
                text=True,
                timeout=10
            ).stdout.strip()

            if not commit_hash:
                print("❌ Нет коммитов для пуша")
                return

            # === 3. Получаем файлы в последнем коммите ===
            diff_result = subprocess.run(
                ["git", "diff", "--name-only", f"{commit_hash}~1", commit_hash],
                cwd=self.current_path,
                capture_output=True,
                text=True,
                timeout=10
            )

            files_to_push = []
            for file_path in diff_result.stdout.strip().split("\n"):
                if file_path:
                    full_path = self.current_path / file_path
                    if full_path.exists():
                        try:
                            content = full_path.read_text(encoding='utf-8')
                            files_to_push.append((file_path, content))
                        except:
                            pass

            if not files_to_push:
                for file_path in changed_files:
                    full_path = self.current_path / file_path
                    if full_path.exists():
                        try:
                            content = full_path.read_text(encoding='utf-8')
                            files_to_push.append((file_path, content))
                        except:
                            pass

            if not files_to_push:
                print("❌ Нет файлов для пуша")
                return

            commit_msg = subprocess.run(
                ["git", "log", "-1", "--pretty=%B"],
                cwd=self.current_path,
                capture_output=True,
                text=True,
                timeout=10
            ).stdout.strip()

            if not commit_msg:
                commit_msg = "Update via GitShell"

            # === 4. Создаём клиент ===
            if not self.git_client:
                self.git_client = GitOverPyCurl(str(self.current_path), self.token)

            owner = self.git_client.owner
            repo_name = self.git_client.repo_name

            # === 5. Проверяем, существует ли репозиторий и ветка ===
            repo_exists = False
            branch_exists = False
            default_branch = "main"

            try:
                # Проверяем, существует ли репозиторий
                repo_info = self.git_client._make_request("GET", f"/repos/{owner}/{repo_name}")
                repo_exists = True
                default_branch = repo_info.get("default_branch", "main")
                print(f"📦 Репозиторий: {owner}/{repo_name} (default: {default_branch})")
            except Exception as e:
                if "404" in str(e):
                    print(f"{Colors.RED}❌ Репозиторий {owner}/{repo_name} не найден{Colors.END}")
                    print(f"   Создайте его на GitHub или проверьте URL")
                    return
                else:
                    print(f"{Colors.YELLOW}⚠️ Не удалось проверить репозиторий: {e}{Colors.END}")

            # === 6. Проверяем, существует ли ветка ===
            try:
                ref_info = self.git_client._make_request(
                    "GET",
                    f"/repos/{owner}/{repo_name}/git/refs/heads/{branch}"
                )
                branch_exists = True
                print(f"🌿 Ветка {branch} существует на GitHub")
            except Exception as e:
                if "404" in str(e) or "409" in str(e):
                    branch_exists = False
                    print(f"{Colors.YELLOW}⚠️ Ветка {branch} не существует на GitHub{Colors.END}")
                else:
                    print(f"{Colors.YELLOW}⚠️ Ошибка проверки ветки: {e}{Colors.END}")

            # === 7. Если репозиторий пустой — создаём первый коммит через API ===
            if not repo_exists or not branch_exists:
                print(f"{Colors.YELLOW}📦 Создаю первый коммит в репозитории...{Colors.END}")

                try:
                    # Создаём начальный коммит напрямую через API
                    # 1. Создаём blob для каждого файла
                    file_map = {}
                    for file_path, content in files_to_push:
                        encoded = base64.b64encode(content.encode("utf-8")).decode("utf-8")
                        blob = self.git_client._make_request(
                            "POST",
                            f"/repos/{owner}/{repo_name}/git/blobs",
                            data={"content": encoded, "encoding": "base64"}
                        )
                        file_map[file_path] = blob["sha"]

                    # 2. Создаём дерево
                    tree_items = []
                    for file_path, blob_sha in file_map.items():
                        tree_items.append({
                            "path": file_path,
                            "mode": "100644",
                            "type": "blob",
                            "sha": blob_sha
                        })

                    new_tree = self.git_client._make_request(
                        "POST",
                        f"/repos/{owner}/{repo_name}/git/trees",
                        data={"tree": tree_items}
                    )

                    # 3. Создаём коммит (без родителей — это первый коммит)
                    new_commit = self.git_client._make_request(
                        "POST",
                        f"/repos/{owner}/{repo_name}/git/commits",
                        data={
                            "message": commit_msg,
                            "tree": new_tree["sha"],
                            "parents": []  # Пустой список = первый коммит
                        }
                    )

                    # 4. Создаём ветку
                    self.git_client._make_request(
                        "POST",
                        f"/repos/{owner}/{repo_name}/git/refs",
                        data={
                            "ref": f"refs/heads/{branch}",
                            "sha": new_commit["sha"]
                        }
                    )

                    print(f"{Colors.GREEN}✅ Первый коммит создан на GitHub!{Colors.END}")
                    print(f"🔗 https://github.com/{owner}/{repo_name}/commit/{new_commit['sha'][:8]}")
                    return

                except Exception as e:
                    print(f"{Colors.RED}❌ Ошибка создания первого коммита: {e}{Colors.END}")
                    print(f"{Colors.YELLOW}💡 Попробуйте вручную: git push -u origin {branch}{Colors.END}")
                    return

            # === 8. Ветка существует — обычный push ===
            print(f"📤 Отправка {len(files_to_push)} файлов через pycurl...")
            success = self.git_client.push_files(
                files_to_push,
                commit_msg,
                branch
            )

            if success:
                print(f"{Colors.GREEN}✅ Пуш выполнен!{Colors.END}")
                print(f"🔗 https://github.com/{owner}/{repo_name}/tree/{branch}")
            else:
                print(f"{Colors.RED}❌ Ошибка пуша через pycurl{Colors.END}")

        except Exception as e:
            print(f"{Colors.RED}❌ Ошибка push: {e}{Colors.END}")
            traceback.print_exc()

    def _cmd_help(self, args):
        help_text = f"""
{Colors.BOLD}{Colors.HEADER}GitShell - Помощь{Colors.END}

{Colors.BOLD}Навигация:{Colors.END}
  cd <папка>     - Перейти в папку
  pwd            - Показать текущую папку
  ls             - Показать содержимое папки

{Colors.BOLD}Git через pycurl:{Colors.END}
  push [ветка]   - Отправить изменения через pycurl

{Colors.BOLD}Настройка:{Colors.END}
  config token <токен>  - Установить GitHub токен
  config delete-token   - Удалить токен
  config                - Показать конфигурацию
  alias <имя> <команда> - Создать сокращение

{Colors.BOLD}Прочее:{Colors.END}
  exit, quit     - Выйти из программы
  help           - Показать эту справку

{Colors.YELLOW}💡 Все остальные Git команды (add, commit, branch, checkout)
   выполняйте в обычной командной строке (CMD){Colors.END}
"""
        print(help_text)

    def _cmd_alias(self, args):
        try:
            if len(args) > 2:
                self.alias[args[1]] = " ".join(args[2:])
                config = {"alias": self.alias}
                CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
                CONFIG_FILE.write_text(json.dumps(config, indent=2), encoding='utf-8')
                print(f"{Colors.GREEN}✅ Alias создан: {args[1]} -> {self.alias[args[1]]}{Colors.END}")
            elif len(args) > 1:
                if args[1] in self.alias:
                    print(self.alias[args[1]])
                else:
                    print(f"{Colors.RED}❌ Alias не найден{Colors.END}")
            else:
                if self.alias:
                    for name, cmd in self.alias.items():
                        print(f"  {name} -> {cmd}")
                else:
                    print("ℹ️ Нет alias")
        except Exception as e:
            print(f"{Colors.RED}❌ Ошибка alias: {e}{Colors.END}")

    def _cmd_git(self, args):
        try:
            code, out, err = self._safe_run_git(args[1:] if len(args) > 1 else [])
            if out:
                print(out)
            if err:
                print(f"{Colors.RED}{err}{Colors.END}")
            return code == 0
        except Exception as e:
            print(f"{Colors.RED}❌ Ошибка git: {e}{Colors.END}")
            return False

    def execute_command(self, cmd: str) -> bool:
        try:
            cmd = cmd.strip()
            if not cmd:
                return True

            parts = cmd.split()

            if parts[0] in self.alias:
                cmd = self.alias[parts[0]] + " " + " ".join(parts[1:])
                parts = cmd.split()

            if parts[0] in ["exit", "quit", "q"]:
                print("👋 До свидания!")
                return False

            if parts[0] == "cd":
                self._cmd_cd(parts)
            elif parts[0] == "pwd":
                self._cmd_pwd(parts)
            elif parts[0] in ["ls", "dir"]:
                self._cmd_ls(parts)
            elif parts[0] == "config":
                self._cmd_config(parts)
            elif parts[0] == "push":
                self._cmd_push(parts)
            elif parts[0] in ["help", "?"]:
                self._cmd_help(parts)
            elif parts[0] == "alias":
                self._cmd_alias(parts)
            elif parts[0] == "git":
                self._cmd_git(parts)
            else:
                try:
                    code, out, err = self._safe_run_git(parts)
                    if out:
                        print(out)
                    if err:
                        print(f"{Colors.RED}{err}{Colors.END}")
                except:
                    print(f"{Colors.RED}❌ Неизвестная команда: {cmd}{Colors.END}")
                    print("Введите 'help' для списка команд")

            return True

        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(f"{Colors.RED}❌ Ошибка: {e}{Colors.END}")
            return True

    def run(self):
        # Заголовок без лишних символов
        print(f"""
{Colors.BOLD}{Colors.HEADER}╔═══════════════════════════════════════════╗
║     GitShell - Неубиваемая оболочка   ║
║     Git через pycurl                  ║
╚═══════════════════════════════════════════╝{Colors.END}

💡 Введите 'help' для списка команд
""")

        if self.token:
            print(f"{Colors.GREEN}🔑 Токен загружен: {self.token[:10]}...{Colors.END}")
        else:
            print(f"{Colors.YELLOW}⚠️  Токен не найден. Используйте: config token <токен>{Colors.END}")

        if not PYCURL_AVAILABLE:
            print(f"{Colors.YELLOW}⚠️  PyCurl не установлен. Push через pycurl не работает.{Colors.END}")

        print()

        while self.running:
            try:
                prompt = self._safe_get_prompt()
                cmd = self._safe_get_input(prompt)

                if not self.execute_command(cmd):
                    break

            except KeyboardInterrupt:
                print("\n")
                continue
            except EOFError:
                print("\n👋 До свидания!")
                break
            except Exception as e:
                print(f"{Colors.RED}❌ Критическая ошибка: {e}{Colors.END}")
                print("   Программа продолжает работу...")

        self._safe_save_history()


# ============================================================
# ТОЧКА ВХОДА
# ============================================================

def main():
    try:
        shell = GitShell()
        shell.run()
    except KeyboardInterrupt:
        print("\n👋 До свидания!")
    except Exception as e:
        print(f"{Colors.RED}❌ Ошибка запуска: {e}{Colors.END}")
        traceback.print_exc()
        input("\nНажмите Enter для выхода...")


if __name__ == "__main__":
    main()