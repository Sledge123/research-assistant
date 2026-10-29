"""经归档复验的批次邮件；持久任务本身就是发送及去重凭据。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, make_msgid, parsedate_to_datetime
import hashlib
import io
import json
import os
if os.name == "nt":
    import msvcrt
else:
    import fcntl
from pathlib import Path, PureWindowsPath
import re
import smtplib
import ssl
import tempfile
import threading
import time
import uuid
import zipfile

from .archive import ArchiveStore, ArchiveError
from .mail_settings import validate_email, validate_mail_settings
from .numbers import parse_code

_KINDS = ("rtf", "txt", "json", "csv")
_STATUSES = {"prepared", "sending", "sent", "failed", "unknown", "skipped", "stopped"}
_THREAD_LOCKS = {}
_LOCK_GUARD = threading.Lock()


class MailError(ValueError):
    """只向调用者提供不含远端原始内容的安全中文错误。"""


def _json(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _sha(payload):
    return hashlib.sha256(payload).hexdigest()


def _file_kinds(files):
    if not isinstance(files,dict):raise MailError('邮件文件集合无效。')
    kinds=_KINDS+(('cif',) if 'cif' in files else ())
    if set(files)!=set(kinds):raise MailError('邮件文件集合无效。')
    return kinds


def _fingerprint(recipient, code, files):
    return _sha(_json({"recipient": recipient, "code": code,
                       "files": {kind: files[kind]["sha256"] for kind in _file_kinds(files)}}))


class MailService:
    def __init__(self, root, settings, *, smtp_factory=None, stop_event=None, submission_guard=None):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise MailError("邮件项目目录无效。")
        self.settings = settings
        self.smtp_factory = smtp_factory or smtplib.SMTP_SSL
        self.stop_event = stop_event or threading.Event()
        self.submission_guard = submission_guard
        self.store = ArchiveStore(self.root)
        with _LOCK_GUARD:
            self._thread_lock = _THREAD_LOCKS.setdefault(str(self.root).casefold(), threading.RLock())

    def _path(self, relative):
        if not isinstance(relative, str) or not relative or "\0" in relative:
            raise MailError("邮件任务路径无效。")
        path = Path(relative)
        if path.anchor or PureWindowsPath(relative).anchor or ".." in path.parts:
            raise MailError("邮件任务必须使用项目内相对路径。")
        target = self.root / path
        if not target.resolve().is_relative_to(self.root):
            raise MailError("邮件任务路径超出项目目录。")
        for parent in (target, *target.parents):
            if parent == self.root:
                break
            if parent.is_symlink():
                raise MailError("邮件任务路径不能为符号链接。")
        return target

    @contextmanager
    def _locked(self):
        with self._thread_lock:
            directory = self._path("output/mail")
            directory.mkdir(parents=True, exist_ok=True)
            with self._path("output/mail/.lock").open("a+b") as stream:
                stream.seek(0, 2)
                if not stream.tell():
                    stream.write(b"0")
                    stream.flush()
                deadline = time.monotonic() + 65
                while True:
                    try:
                        stream.seek(0)
                        if os.name == "nt":
                            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise MailError("另一邮件任务正在运行，请稍后重试。") from None
                        time.sleep(.05)
                try:
                    yield
                finally:
                    stream.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def _atomic(self, relative, payload):
        target = self._path(relative)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", dir=target.parent, prefix=".mail-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _write_job(self, job):
        job["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._atomic(job["job_path"], _json(job))

    def _job_paths(self, job_id):
        if not isinstance(job_id, str) or re.fullmatch(r"[0-9]{8}-[0-9]{6}-[a-f0-9]{12}", job_id) is None:
            raise MailError("邮件任务编号无效。")
        base = f"output/mail/{job_id}"
        return {"job_path": f"{base}/job.json", "message_path": f"{base}/message.eml", "zip_path": f"{base}/cards.zip"}

    def _read_job(self, job_id):
        try:
            paths = self._job_paths(job_id)
            job = json.loads(self._path(paths["job_path"]).read_bytes())
            if (not isinstance(job, dict) or job.get("schema_version") != 1 or job.get("job_id") != job_id
                    or job.get("status") not in _STATUSES or any(job.get(name) != path for name, path in paths.items())
                    or not isinstance(job.get("cards"), list)):
                raise ValueError("invalid")
            return job
        except (OSError, UnicodeError, ValueError, TypeError):
            raise MailError("邮件任务记录无法校验，已禁止发送；请保留任务目录排查。") from None

    def _jobs(self):
        jobs = []
        for directory in sorted(self._path("output/mail").iterdir()):
            if directory.name.startswith("."):
                continue
            if directory.is_dir() and self._path(f"output/mail/{directory.name}/job.json").is_file():
                job = self._read_job(directory.name)
                if job["status"] == "sending":
                    job.update(status="unknown", message="上次发送在确认前中断，状态不确定；请核对邮箱后明确选择重新发送。")
                    self._write_job(job)
                if job["status"] in {"sent", "unknown"}:
                    # 去重凭据损坏时关闭自动投递，不能静默遗失已发送证据。
                    self._validate_package(job, check_source=False)
                jobs.append(job)
        return jobs

    def latest_job(self):
        with self._locked():
            jobs = self._jobs()
            def created_key(job):
                for name in ("created_at", "updated_at", "date"):
                    try:
                        value = job.get(name)
                        parsed = parsedate_to_datetime(value) if name == "date" else datetime.fromisoformat(value)
                        return parsed.astimezone(timezone.utc).isoformat(), job["job_id"]
                    except (TypeError, ValueError, AttributeError):
                        continue
                return "", job["job_id"]
            return max(jobs, key=created_key) if jobs else None

    def _snapshot(self, receipt_path, recipient):
        try:
            receipt = self.store.validate(self._path(receipt_path))
            payloads = {}
            files = {}
            for kind in _file_kinds(receipt['files']):
                item = receipt["files"][kind]
                payload = self._path(item["path"]).read_bytes()
                if len(payload) != item["size"] or _sha(payload) != item["sha256"]:
                    raise ValueError("changed")
                payloads[f"{receipt['code']}/{receipt['code']}.{kind}"] = payload
                files[kind] = dict(item)
            receipt_bytes = self._path(receipt_path).read_bytes()
            if json.loads(receipt_bytes) != receipt or self.store.validate(self._path(receipt_path)) != receipt:
                raise ValueError("changed")
            payloads[f"{receipt['code']}/receipt.json"] = receipt_bytes
            card = json.loads(payloads[f"{receipt['code']}/{receipt['code']}.json"])
            entry = {"code": receipt["code"], "phase_name": card["phase_name"],
                     "chemical_formula": card.get("chemical_formula") or "未提供", "peak_count": card["peak_count"],
                     "receipt_path": receipt_path, "receipt_sha256": _sha(receipt_bytes), "files": files,
                     "fingerprint": _fingerprint(recipient, receipt["code"], files)}
            if card.get('data_type')=='jade_structure_data':
                entry.update(data_type=card['data_type'],site_count=len(card['structure']['sites']))
            return entry, payloads
        except (ArchiveError, OSError, TypeError, ValueError, KeyError):
            raise MailError("源归档未通过完整校验或在读取时发生变化。") from None

    def _summary(self, result):
        safe = {}
        for name in ("code", "input_code", "actual_code", "status", "message"):
            value = str(result.get(name) or "").replace(str(self.root), ".")
            # 归档消息是本地信息；限制长度及控制字符，避免污染邮件。
            safe[name] = " ".join(value.split())[:1000]
        return safe

    @staticmethod
    def _body(job):
        structure=any(card.get('data_type')=='jade_structure_data' for card in job['cards'])
        rows = ["晶体结构与卡片归档汇总" if structure else "HighScore 卡片归档汇总", "", f"通过完整校验并附于 ZIP 的卡片：{len(job['cards'])} 张", ""]
        for index, card in enumerate(job["cards"], 1):
            count=f"原子位点数：{card['site_count']}" if card.get('data_type')=='jade_structure_data' else f"峰数：{card['peak_count']}"
            rows.append(f"{index}. 编号：{card['code']}；物相：{card['phase_name']}；化学式：{card['chemical_formula']}；{count}")
        rows += ["", "本批其余处理结果及说明："]
        for record in job["results"]:
            label = {"success": "归档成功", "skipped": "已有归档", "failed": "归档失败", "not_found": "未找到",
                     "stopped": "已停止", "pending": "未开始"}.get(record["status"], record["status"])
            rows.append(f"{record['actual_code'] or record['input_code'] or record['code']}：{label}；{record['message']}")
        for note in job.get("exclusions", []):
            rows.append(note)
        rows += ["", "附件仅包含重新校验通过的卡片归档。邮件状态“已发送”表示 SMTP 服务器已接受，并非收件箱送达证明。", ""]
        return "\n".join(rows)

    def _create(self, cards, payloads, results, recipient, *, dry_run=False, exclusions=None, supersedes=None):
        job_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:12]
        paths = self._job_paths(job_id)
        self._path(f"output/mail/{job_id}").mkdir()
        sender = validate_email(self.settings.username)
        job = {"schema_version": 1, "job_id": job_id, **paths, "sender": sender, "recipient": recipient,
               "created_at": datetime.now(timezone.utc).isoformat(timespec="microseconds"),
               "message_id": make_msgid(domain=sender.split("@")[1]), "date": format_datetime(datetime.now(timezone.utc)),
               "status": "prepared", "message": "离线验证通过，尚未连接邮件服务器。" if dry_run else "邮件已准备，尚未发送。",
               "dry_run": bool(dry_run), "cards": cards, "results": results, "exclusions": exclusions or []}
        if supersedes:
            job["supersedes"] = supersedes
        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, payload in sorted(payloads.items()):
                archive.writestr(name, payload)
        zip_bytes = archive_bytes.getvalue()
        message = EmailMessage(policy=policy.SMTP)
        message["From"] = sender
        message["To"] = recipient
        message["Subject"] = f"HighScore 卡片归档汇总（{len(cards)} 张）"
        message["Date"] = job["date"]
        message["Message-ID"] = job["message_id"]
        message.set_content(self._body(job), charset="utf-8")
        message.add_attachment(zip_bytes, maintype="application", subtype="zip", filename="cards.zip")
        eml_bytes = message.as_bytes()
        job.update(zip_sha256=_sha(zip_bytes), message_sha256=_sha(eml_bytes))
        self._atomic(job["zip_path"], zip_bytes)
        self._atomic(job["message_path"], eml_bytes)
        self._validate_package(job, check_source=True)
        self._write_job(job)
        return job

    def _validate_package(self, job, *, check_source):
        try:
            if validate_email(job["recipient"]) != job["recipient"] or validate_email(job["sender"]) != job["sender"]:
                raise ValueError("address")
            if any(job.get(name) != value for name, value in self._job_paths(job["job_id"]).items()):
                raise ValueError("path")
            eml_bytes, zip_bytes = (self._path(job[name]).read_bytes() for name in ("message_path", "zip_path"))
            if _sha(eml_bytes) != job["message_sha256"] or _sha(zip_bytes) != job["zip_sha256"]:
                raise ValueError("hash")
            message = BytesParser(policy=policy.default).parsebytes(eml_bytes)
            expected_headers = {"From": job["sender"], "To": job["recipient"], "Message-ID": job["message_id"],
                                "Subject": f"HighScore 卡片归档汇总（{len(job['cards'])} 张）", "Date": job["date"]}
            if message.defects or message.get_all("Bcc") or message.get_all("Cc"):
                raise ValueError("headers")
            if any(len(message.get_all(name, [])) != 1 or str(message[name]) != value for name, value in expected_headers.items()):
                raise ValueError("headers")
            body = message.get_body(preferencelist=("plain",))
            if body is None or body.get_content().replace("\r\n", "\n") != self._body(job):
                raise ValueError("body")
            attachments = list(message.iter_attachments())
            if (len(attachments) != 1 or attachments[0].get_filename() != "cards.zip"
                    or attachments[0].get_content_type() != "application/zip"
                    or attachments[0].get_payload(decode=True) != zip_bytes):
                raise ValueError("attachment")
            expected = {}
            fingerprints = set()
            for card in job["cards"]:
                code = card["code"]
                if card["receipt_path"] != f"output/cards/{code}/receipt.json":
                    raise ValueError("receipt")
                if card["fingerprint"] != _fingerprint(job["recipient"], code, card["files"]) or card["fingerprint"] in fingerprints:
                    raise ValueError("fingerprint")
                fingerprints.add(card["fingerprint"])
                for kind in _file_kinds(card['files']):
                    item = card["files"][kind]
                    if item["path"] != f"output/cards/{code}/{code}.{kind}":
                        raise ValueError("sourcepath")
                    expected[f"{code}/{code}.{kind}"] = item["sha256"]
                expected[f"{code}/receipt.json"] = card["receipt_sha256"]
                if check_source:
                    current, _ = self._snapshot(card["receipt_path"], job["recipient"])
                    if current != card:
                        raise ValueError("sourcechanged")
            if not expected:
                raise ValueError("empty")
            with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
                if len(archive.infolist()) != len(expected) or set(archive.namelist()) != set(expected) or archive.testzip():
                    raise ValueError("members")
                if any(_sha(archive.read(name)) != digest for name, digest in expected.items()):
                    raise ValueError("ziphash")
                for card in job["cards"]:
                    content = json.loads(archive.read(f"{card['code']}/{card['code']}.json"))
                    if any(content.get(name) != card[name] for name in ("code", "phase_name", "peak_count")):
                        raise ValueError("metadata")
                    if (content.get("chemical_formula") or "未提供") != card["chemical_formula"]:
                        raise ValueError("formula")
                    if card.get('data_type')=='jade_structure_data' and (content.get('data_type')!=card['data_type']
                            or len(content['structure']['sites'])!=card['site_count']):raise ValueError('structure')
            return message
        except (OSError, ValueError, TypeError, KeyError, AttributeError, zipfile.BadZipFile, RuntimeError):
            raise MailError("邮件产物或源归档校验失败，已禁止发送；请保留任务目录排查。") from None

    def prepare(self, results, recipient):
        return self._prepare(results, recipient, dry_run=False)

    def _prepare(self, results, recipient, *, dry_run):
        recipient = validate_email(recipient)
        validate_mail_settings(self.settings, recipient, require_credentials=False)
        if self.stop_event.is_set():
            return {"status": "stopped", "message": "归档已停止，未启动邮件投递。", "recipient": recipient}
        with self._locked():
            jobs = self._jobs()
            blocked = {card["fingerprint"] for job in jobs
                       if (job["status"] in {"sent", "unknown", "sending"}
                           or (not job.get("dry_run") and job["status"] == "prepared"))
                       for card in job["cards"]}
            cards, payloads, exclusions, summaries = [], {}, [], []
            seen = set()
            for result in results:
                if not isinstance(result, dict):
                    continue
                summaries.append(self._summary(result))
                if result.get("status") not in {"success", "skipped"}:
                    continue
                try:
                    entry, content = self._snapshot(result.get("receipt_path"), recipient)
                    for identity in (result.get("actual_code"), result.get("input_code"), result.get("code")):
                        if identity and not parse_code(identity).matches(entry["code"]):
                            raise MailError("结果编号与归档身份不一致。")
                except ValueError:
                    exclusions.append(f"{result.get('actual_code') or result.get('code') or '卡片'}：源归档校验未通过，未加入附件。")
                    continue
                fingerprint = entry["fingerprint"]
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                if fingerprint in blocked and not dry_run:
                    exclusions.append(f"{entry['code']}：相同内容已发送或已有待处理／状态不确定的邮件任务，未重复加入。")
                    continue
                cards.append(entry)
                payloads.update(content)
            if self.stop_event.is_set():
                return {"status": "stopped", "message": "已停止，未启动邮件投递。", "recipient": recipient}
            if not cards:
                return {"status": "skipped", "message": "没有需要发送且通过完整校验的新卡片；未连接邮件服务器。",
                        "recipient": recipient, "cards": [], "exclusions": exclusions}
            return self._create(cards, payloads, summaries, recipient, dry_run=dry_run, exclusions=exclusions)

    def deliver(self, results, recipient, dry_run=False):
        job = self._prepare(results, recipient, dry_run=dry_run)
        if job["status"] != "prepared" or dry_run:
            return job
        return self.send(job["job_id"])

    def _set_state(self, job, status, message):
        job.update(status=status, message=message)
        self._write_job(job)
        return job

    def send(self, job_id, *, force=False):
        with self._locked():
            jobs = self._jobs()
            job = next((item for item in jobs if item["job_id"] == job_id), None)
            if job is None:
                job = self._read_job(job_id)
            followed = set()
            while not force and job.get("replacement_job_id"):
                if job["job_id"] in followed:
                    raise MailError("邮件接续任务存在循环，已禁止发送。")
                followed.add(job["job_id"])
                job_id = job["replacement_job_id"]
                job = next((item for item in jobs if item["job_id"] == job_id), None) or self._read_job(job_id)
            if not force and job["status"] in {"sent", "unknown"}:
                return job
            if self.stop_event.is_set():
                if job["status"] in {"sent", "unknown"}:
                    return job
                return self._set_state(job, "stopped", "已停止，未启动新的邮件发送。")
            fingerprints = {card["fingerprint"] for card in job["cards"]}
            uncertain = [other["job_id"] for other in jobs if other["job_id"] != job_id
                         and other["status"] in {"unknown", "sending"}
                         and any(card["fingerprint"] in fingerprints for card in other["cards"])]
            if uncertain and not force:
                job["blocked_by"] = uncertain
                return self._set_state(job, "unknown", "同内容已有状态不确定的邮件任务，本任务未提交；请核对邮箱后明确重新发送。")
            sent = {card["fingerprint"] for other in jobs if other["status"] == "sent" and other["job_id"] != job_id
                    for card in other["cards"]}
            remaining = [card for card in job["cards"] if force or card["fingerprint"] not in sent]
            try:
                # 先核对旧任务全部产物；逐卡去重后仅复验仍要投递的源归档。
                message = self._validate_package(job, check_source=False)
                if not remaining:
                    return self._set_state(job, "skipped", "任务中全部卡片已由其他邮件接受，未重复发送。")
                settings = validate_mail_settings(self.settings, job["recipient"])
                if settings.username != job["sender"]:
                    raise MailError("当前 SMTP 用户与任务发件人不一致，请恢复原邮件账户设置。")
                payloads = {}
                for card in remaining:
                    current, content = self._snapshot(card["receipt_path"], job["recipient"])
                    if current != card:
                        raise MailError("源归档与任务指纹不一致。")
                    payloads.update(content)
                if force or len(remaining) != len(job["cards"]):
                    previous = job
                    exclusions = list(previous.get("exclusions", []))
                    if not force:
                        exclusions += [f"{card['code']}：相同内容已由另一邮件接受，本次重试未重复加入附件。"
                                       for card in previous["cards"] if card["fingerprint"] in sent]
                    job = self._create(copy_cards(remaining), payloads, previous["results"], previous["recipient"],
                                       exclusions=exclusions, supersedes=previous["job_id"])
                    if not force:
                        previous["replacement_job_id"] = job["job_id"]
                        self._set_state(previous, "skipped", "已逐卡去重；未发送卡片转入接续邮件任务。")
                    message = self._validate_package(job, check_source=True)
            except ValueError:
                if job["status"] == "sent":
                    return {**job, "status": "failed", "original_status": "sent",
                            "message": "重新发送前校验失败；原来的服务器接受记录仍保留。"}
                status = "unknown" if job["status"] == "unknown" else "failed"
                return self._set_state(job, status, "邮件配置、产物或源归档校验失败，未连接邮件服务器。")
            smtp, submitted, accepted = None, False, False
            try:
                # 先持久化 sending；一旦进程崩溃，下次只能恢复为 unknown。
                job["dry_run"] = False
                self._set_state(job, "sending", "邮件正在投递，等待服务器确认。")
                if self.stop_event.is_set():
                    return self._set_state(job, "stopped", "已停止，未连接邮件服务器。")
                smtp = self.smtp_factory(settings.smtp_host, settings.smtp_port, timeout=30, context=ssl.create_default_context())
                smtp.login(settings.username, settings.authorization_code)
                if self.submission_guard is not None and self.submission_guard() is False:
                    self.stop_event.set()
                if self.stop_event.is_set():
                    return self._set_state(job, "stopped", "已停止，未提交邮件。")
                submitted = True
                refused = smtp.send_message(message, from_addr=job["sender"], to_addrs=[job["recipient"]])
                if refused:
                    return self._set_state(job, "failed", "SMTP 服务器明确拒绝收件人；可检查设置后重试。")
                accepted = True
                return self._set_state(job, "sent", "SMTP 服务器已接受邮件；尚无收件箱送达证明。")
            except (smtplib.SMTPAuthenticationError, smtplib.SMTPSenderRefused, smtplib.SMTPRecipientsRefused,
                    smtplib.SMTPDataError, smtplib.SMTPConnectError, smtplib.SMTPHeloError) as error:
                code = getattr(error, "smtp_code", None)
                suffix = f"（SMTP {code}）" if type(code) is int and 100 <= code <= 599 else ""
                return self._set_state(job, "failed", f"SMTP 服务器明确拒绝本次投递{suffix}；可检查设置后重试。")
            except Exception:
                status = "unknown" if submitted or accepted else "failed"
                explanation = ("邮件提交后未能可靠保存服务器确认，状态不确定；请核对邮箱后明确选择重新发送。"
                               if status == "unknown" else "连接或认证未完成，邮件尚未提交；可检查设置后重试。")
                job.update(status=status, message=explanation)
                try:
                    self._write_job(job)
                except Exception:
                    # 磁盘原有 sending 回执保留，重启后仍禁止自动重发。
                    pass
                return job
            finally:
                if smtp is not None:
                    try:
                        smtp.quit()
                    except Exception:
                        try:
                            smtp.close()
                        except Exception:
                            pass


def copy_cards(cards):
    return json.loads(json.dumps(cards, ensure_ascii=False))
