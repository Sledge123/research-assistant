"""科研通 Linux Playwright 适配；只使用正常网页会话与站内下载流程。"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from .engine import LiteratureError, LeaseLost, SliceExpired, candidate_identity, clean_doi
from .pdf import MAX_PDF_BYTES, extract_dois, normalize_title

ORIGIN = 'https://www.ablesci.com'
MY_ASSISTS = ORIGIN + '/my/assist-my'
CREATE = ORIGIN + '/assist/create'
EXTRACTION_LABELS = ('信息正确，直接发布', '信息正确，还需修改')
LOADING = ('正在查询', '查询状态', '请稍候', '正在检查', '正在提交', '加载中')


@dataclass(frozen=True)
class RequestRecord:
    id: str
    title: str
    status: str = ''


@dataclass(frozen=True)
class Listing:
    records: list
    complete: bool
    resumable: bool = False


@dataclass(frozen=True)
class Attachment:
    id: str
    name: str
    needs_accept: bool = True


@dataclass(frozen=True)
class SourceState:
    status: str
    attachment: Attachment | None = None
    evidence_sha256: str = ''


def site_url(value, path=None, identifier=None):
    """拒绝凭据、额外下载参数及其它域名，不把带 token 的链接传入日志。"""
    try:
        parsed = urlsplit(urljoin(ORIGIN, value))
        if (parsed.scheme != 'https' or parsed.hostname not in {'www.ablesci.com', 'ablesci.com'}
                or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443)
                or parsed.fragment or (path is not None and parsed.path.rstrip('/') != path.rstrip('/'))):
            raise ValueError()
        if identifier is not None:
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', identifier) or parse_qs(parsed.query) != {'id': [identifier]}:
                raise ValueError()
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise LiteratureError('科研通页面地址或附件绑定无法核验，已停止。') from None


def request_id(value):
    parsed = site_url(value, '/assist/detail')
    query = parse_qs(parsed.query)
    values = query.get('id', [])
    if set(query) != {'id'} or len(values) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', values[0]):
        raise LiteratureError('科研通详情页没有唯一可核验的求助 ID。')
    return values[0]


def extracted_identity(message):
    match = re.search(r'(?:^|\s)标题\s*(.*?)\s+DOI(?:\s|$)', message, re.I | re.S)
    if not match:
        raise LiteratureError('智能提取弹窗没有可核验的论文标题。')
    title = re.sub(r'\s+相似度\s*\d+(?:\.\d+)?%\s*$', '', match.group(1), flags=re.I).strip()
    match = re.search(r'\b10\.\d{4,9}/[^\s<>"\x00-\x1f]+', message, re.I)
    return title, clean_doi(match.group(0).rstrip('，。;,') if match else None)


def platform_identity_matches(message, selected):
    """只认可明确标注的期刊/年份字段，不用页脚年份或任意正文提及代替。"""
    selected = candidate_identity(selected)
    if selected['doi']:
        return True
    if not selected['journal'] or not selected['year']:
        return False
    lines = [line.strip() for line in message.splitlines() if line.strip()]
    values = {'journal': [], 'year': []}
    labels = {'journal': r'(?:期刊(?:名称)?|刊物(?:名称)?|Journal|Publication)',
              'year': r'(?:出版年(?:份)?|年份|Year|Publication\s+year)'}
    for index, line in enumerate(lines):
        for key, pattern in labels.items():
            match = re.fullmatch(pattern+r'\s*[:：]?\s+(.+)', line, re.I)
            if match:
                values[key].append(match.group(1).strip())
            elif re.fullmatch(pattern+r'\s*[:：]?', line, re.I) and index+1 < len(lines):
                values[key].append(lines[index+1])
    journal_ok = any(normalize_title(value) == normalize_title(selected['journal']) for value in values['journal'])
    year_ok = any(re.fullmatch(str(selected['year'])+r'(?:\s*年)?', value) for value in values['year'])
    return journal_ok and year_ok


def detail_publication_dois(message):
    """只读本文元数据区的明确 DOI 字段，排除参考文献、摘要和评论。"""
    prefix = re.split(r'(?im)(?:^|\n)\s*(?:\d+[.\s]+)?(?:references|bibliography|abstract|introduction|参考文献|摘要|引言|应助文件|应助记录|评论)(?=\s|[:：.]|$)', message, maxsplit=1)[0]
    lines = [line.strip() for line in prefix[:16000].splitlines() if line.strip()]
    result = set()
    for index, line in enumerate(lines):
        match = re.fullmatch(r'(?:DOI|Digital\s+object\s+identifier)\s*[:：]?\s*(.*)', line, re.I)
        if not match:
            continue
        value = match.group(1).strip()
        if not value and index+1 < len(lines):
            value = lines[index+1]
        # 仅移除网页明确 DOI 字段尾部的固定复制控件文字。
        value = value.removesuffix(' 复制 doi')
        # 字段只能是一个完整 DOI/doi.org 地址，不能是含该 DOI 的一句叙述。
        if re.fullmatch(r'(?:https?://(?:dx\.)?doi\.org/)?10\.\d{4,9}/[^\s<>"\']+', value, re.I):
            result.update(extract_dois(value))
    return result


class AblesciBrowser:
    """每轮独立浏览器；私有会话 0600，无截图、trace、HAR 或原始站点日志。"""
    def __init__(self, root, account, store, guard):
        self.root, self.account, self.store, self.guard = Path(root).resolve(), account, store, guard
        self.manager = self.playwright = self.browser = self.context = self.page = None
        self.temporary = None
        self.previous_environment = {}
        self.unexpected_dialog = False
        self.unexpected_page = False
        self.identity = None
        self._scan_proof = None

    def _timeout_ms(self, maximum=12000):
        self.guard()
        getter = getattr(self.guard, 'remaining_seconds', None)
        value = getter() if callable(getter) else None
        if isinstance(value, (int, float)) and math.isfinite(value):
            if value <= 0:
                raise SliceExpired()
            maximum = min(maximum, value*1000)
        return max(1, int(maximum))

    def _pause(self, maximum=200, until=None):
        if until is not None:
            maximum = min(maximum, max(1, int((until-time.monotonic())*1000)))
        self.page.wait_for_timeout(self._timeout_ms(maximum))

    def bind_identity(self, selected):
        self.identity = candidate_identity(selected)

    def verify_identity(self, identifier, title, doi):
        self._goto(ORIGIN+'/assist/detail?'+urlencode({'id': identifier}), '/assist/detail')
        self._clear_notification()
        self._verify_detail(identifier, title, doi)
        return True

    def __enter__(self):
        try:
            # web 镜像和离线测试可以安全导入本模块，无需安装 Playwright。
            from playwright.sync_api import sync_playwright
            self.guard()
            directory = self.store.path('output/literature-worker/browser')
            self.store._directory(directory)
            self.temporary = tempfile.TemporaryDirectory(dir=directory, prefix='session-')
            self.runtime = Path(self.temporary.name)
            os.chmod(self.runtime, 0o700)
            for name in ('TMPDIR', 'TEMP', 'TMP'):
                self.previous_environment[name] = os.environ.get(name)
                os.environ[name] = str(self.runtime)
            for name, child in (('XDG_CACHE_HOME', 'cache'), ('XDG_CONFIG_HOME', 'config')):
                self.previous_environment[name] = os.environ.get(name)
                os.environ[name] = str(self.runtime / child)
            os.environ.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(self.store.path('.cache/literature/playwright-browsers')))
            self.manager = sync_playwright()
            self.playwright = self.manager.start()
            self.browser = self.playwright.chromium.launch(headless=True, downloads_path=str(self.runtime/'downloads'), timeout=self._timeout_ms(20000))
            session = self.store.session_path(self.account['account_ref'])
            state = None
            if session.exists():
                if not session.is_file() or session.stat().st_size > 2*1024*1024 or (os.name != 'nt' and session.stat().st_mode & 0o077):
                    raise LiteratureError('科研通会话文件权限或内容无法核验。')
                try:
                    state = json.loads(session.read_text(encoding='utf-8'))
                except (ValueError, OSError, UnicodeError):
                    raise LiteratureError('科研通私有会话无法读取，请管理员恢复。') from None
            self.context = self.browser.new_context(storage_state=state, accept_downloads=True,
                viewport={'width': 1280, 'height': 900}, locale='zh-CN', timezone_id='Asia/Shanghai')
            self.page = self.context.new_page()
            self.page.set_default_timeout(self._timeout_ms(12000))
            self.page.set_default_navigation_timeout(self._timeout_ms(20000))
            self.page.on('dialog', self._dialog)
            self.context.on('page', self._new_page)
            return self
        except (LeaseLost, SliceExpired, LiteratureError):
            self.__exit__(None, None, None)
            raise
        except ImportError:
            self.__exit__(None, None, None)
            raise LiteratureError('文献执行器缺少 Playwright 环境，请管理员检查部署。') from None
        except Exception:
            self.__exit__(None, None, None)
            raise LiteratureError('科研通浏览器暂时无法启动，将稍后重试。', 'waiting') from None

    def __exit__(self, *_):
        for resource in (self.context, self.browser):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception:
                pass
        if self.temporary is not None:
            self.temporary.cleanup()
        for name, previous in self.previous_environment.items():
            if previous is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = previous

    def _dialog(self, dialog):
        self.unexpected_dialog = True
        try:
            dialog.dismiss()
        except Exception:
            pass

    def _new_page(self, page):
        if page != self.page:
            self.unexpected_page = True
            try:
                page.close()
            except Exception:
                pass

    @staticmethod
    def _visible(locator):
        return [locator.nth(index) for index in range(min(locator.count(), 100)) if locator.nth(index).is_visible()]

    def _security(self):
        self.guard()
        if self.unexpected_dialog or self.unexpected_page:
            raise LiteratureError('科研通出现未知弹窗或新窗口，已停止自动操作。')
        site_url(self.page.url)
        challenge = self.page.locator("#vericode, [id*='captcha' i], [class*='captcha' i], [data-sitekey], iframe[src*='captcha' i]")
        if self._visible(challenge) or self._visible(self.page.get_by_text('验证码', exact=True)):
            raise LiteratureError('科研通要求验证码或安全验证，请管理员人工处理。')
        # 只检测明确风控提示，不因正常的“邮箱验证码登录”链接误报。
        if self._visible(self.page.get_by_text(re.compile(r'操作过于频繁|访问受限|安全验证|异常访问'))):
            raise LiteratureError('科研通提示安全限制，已停止自动操作。')

    def _goto(self, value, path=None):
        site_url(value, path)
        self.guard()
        try:
            self.page.goto(value, wait_until='domcontentloaded', timeout=self._timeout_ms(20000))
        except Exception:
            self.guard()
            raise LiteratureError('科研通页面暂时无法打开，将稍后重试。', 'waiting') from None
        self._security()
        if path is not None:
            parsed = site_url(self.page.url)
            if parsed.path.rstrip('/') != path.rstrip('/'):
                if '/site/login' in parsed.path:
                    raise LiteratureError('科研通登录状态已失效，请管理员检查账号。')
                raise LiteratureError('科研通页面跳转与预期不符，已停止。')

    def _wait(self, predicate, seconds=15, message='科研通页面状态无法核验。', state='needs_attention'):
        deadline = time.monotonic() + seconds
        while True:
            self._security()
            result = predicate()
            if result:
                return result
            if time.monotonic() >= deadline:
                raise LiteratureError(message, state)
            self._pause(200, deadline)

    def _layer(self):
        layers = self._visible(self.page.locator('.layui-layer'))
        if not layers:
            return None, '', []
        layer = layers[-1]
        contents = layer.locator('.layui-layer-content')
        text = contents.inner_text(timeout=self._timeout_ms()).strip() if contents.count() == 1 else layer.inner_text(timeout=self._timeout_ms()).strip()
        labels = [' '.join(node.inner_text(timeout=self._timeout_ms()).split()) for node in self._visible(layer.locator('.layui-layer-btn a, .layui-layer-btn button'))]
        return layer, text[:12000], labels

    def _click(self, control):
        self._security()
        self.guard(force=True)
        if not control.is_visible() or not control.is_enabled(timeout=self._timeout_ms()):
            raise LiteratureError('目标站点控件不可见或不可用，已停止。')
        control.click(timeout=self._timeout_ms())
        self._security()

    def _fill(self, control, value):
        self._security()
        if not control.is_visible() or not control.is_enabled(timeout=self._timeout_ms()):
            raise LiteratureError('站点输入控件不可用，已停止。')
        control.focus(timeout=self._timeout_ms())
        if not control.evaluate('(element) => element === document.activeElement', timeout=self._timeout_ms()):
            raise LiteratureError('站点输入焦点无法核验，已停止。')
        control.fill(value, timeout=self._timeout_ms())

    def _click_layer(self, label):
        layer, _, labels = self._layer()
        if layer is None or labels.count(label) != 1:
            raise LiteratureError('站点弹窗按钮与预期不符，已停止。')
        buttons = self._visible(layer.locator('.layui-layer-btn a, .layui-layer-btn button').filter(has_text=re.compile('^'+re.escape(label)+'$')))
        if len(buttons) != 1:
            raise LiteratureError('无法唯一定位站点确认按钮。')
        self._click(buttons[0])

    def _clear_notification(self):
        _, message, labels = self._layer()
        if not message and not labels:
            return
        if '已经有人上传了文件' in message and labels == ['确定']:
            self._click_layer('确定')
            self._wait(lambda: not self._layer()[0], seconds=8, message='应助通知未正常关闭。')
            return
        raise LiteratureError('科研通出现未知提示，已停止自动操作。')

    def _login_controls(self):
        variants = (
            ('#LAY-user-login2-email', '#LAY-user-login2-password', 'button#able-login-submit, button[lay-filter="login2-submit"]'),
            ('#LAY-user-login-email', '#LAY-user-login-password', 'button[lay-filter="do-submit"]'),
            ('#login-form input[name="email"]', '#login-form input[name="password"]', '#login-form button[type="submit"], #able-login-submit'),
            ('input[name="email"]', 'input[name="password"]', '#able-login-submit'),
        )
        for selectors in variants:
            controls = [self._visible(self.page.locator(selector)) for selector in selectors]
            if all(len(items) == 1 for items in controls):
                return tuple(items[0] for items in controls)
        return None

    def _list_ready(self):
        if '/site/login' in urlsplit(self.page.url).path or self._login_controls():
            return False
        return bool(self.page.locator('table.layui-table tbody').count())

    def login(self):
        self.guard()
        session = self.store.session_path(self.account['account_ref'])
        if session.exists():
            self._goto(MY_ASSISTS)
            if self._list_ready():
                self._clear_notification()
                return
        self._goto(ORIGIN+'/site/login')
        if '/site/login' not in urlsplit(self.page.url).path:
            self._goto(MY_ASSISTS, '/my/assist-my')
            if self._list_ready():
                return
        controls = self._wait(self._login_controls, seconds=12, message='科研通新旧登录表单均无法核验，请管理员检查。')
        self._clear_notification()
        email, password, submit = controls
        self._fill(email, self.account['username'])
        self._fill(password, self.account['password'])
        self._click(submit)
        self._wait(lambda: not self._login_controls() or bool(self._visible(self.page.get_by_text('已成功登录科研通账户', exact=False))),
                   seconds=18, message='科研通未确认登录成功，请管理员检查账号或安全验证。')
        self._goto(MY_ASSISTS, '/my/assist-my')
        if not self._list_ready():
            raise LiteratureError('登录后未能核验我的求助页面，已停止。')
        self._clear_notification()
        self.store.save_session(self.account['account_ref'], self.context.storage_state())

    def _records(self):
        if not self._list_ready():
            raise LiteratureError('我的求助列表结构无法核验，禁止新发。')
        records = []
        rows = self.page.locator('table.layui-table tbody tr.list-item')
        if rows.count() > 1000:
            raise LiteratureError('我的求助单页记录过多，未完整核验分页。')
        for index in range(rows.count()):
            self.guard()
            row = rows.nth(index)
            anchors = row.locator('td.list-subject a[href*="/assist/detail?id="]')
            if anchors.count() != 1:
                raise LiteratureError('我的求助存在无法唯一识别的详情链接，禁止新发。')
            anchor = anchors.first
            title = ' '.join(anchor.inner_text(timeout=self._timeout_ms()).split())
            if not title:
                raise LiteratureError('我的求助存在空题名，无法确认去重结果。')
            identifier = request_id(anchor.get_attribute('href', timeout=self._timeout_ms()))
            badges = row.locator('td.list-satus-item .assist-badge')
            status = ' '.join(badges.first.inner_text(timeout=self._timeout_ms()).split()) if badges.count() else ''
            records.append(RequestRecord(identifier, title, status))
        # 防止新版列表改了行 class 后将非空表误判为空。
        if not records:
            body = self.page.locator('table.layui-table tbody').inner_text(timeout=self._timeout_ms()).strip()
            if body and not any(value in body for value in ('暂无', '没有', '无数据', '无记录')):
                raise LiteratureError('我的求助非空但记录结构已变化，禁止新发。')
        return records

    def _page_href(self, href):
        parsed = site_url(urljoin(self.page.url, href), '/my/assist-my')
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (set(query)-{'page', 'per-page'} or len(query.get('page', [])) != 1
                or not re.fullmatch(r'[1-9][0-9]{0,3}', query['page'][0])):
            raise LiteratureError('我的求助分页链接包含未知参数或无效页号，禁止新发。')
        per_page = query.get('per-page', [])
        if per_page and (len(per_page) != 1 or not re.fullmatch(r'[1-9][0-9]{0,2}', per_page[0])):
            raise LiteratureError('我的求助分页条数无法核验，禁止新发。')
        return parsed.path+'?'+parsed.query, int(query['page'][0]), per_page[0] if per_page else None

    def _attribute(self, control, name):
        return control.get_attribute(name, timeout=self._timeout_ms())

    def _pagination(self):
        """只跟随页面实际给出的 href；HTML 末页须看到禁用的 next/last li。"""
        # 分页只读核验依据 DOM；DCL 后尚未绘制的元数据也必须计入唯一性检查。
        layui = self.page.locator('.layui-laypage')
        html = self.page.locator('.pagination')
        if layui.count() and html.count():
            raise LiteratureError('我的求助同时出现两套分页结构，禁止新发。')
        if html.count():
            if html.count() != 1:
                raise LiteratureError('我的求助 HTML 分页不唯一，禁止新发。')
            pager = html.nth(0)
            active = pager.locator('li.active')
            next_items = pager.locator('li.next')
            last_items = pager.locator('li.last')
            if any(items.count() != 1 for items in (active, next_items, last_items)):
                raise LiteratureError('我的求助 HTML 当前页、下页或尾页结构缺失，禁止新发。')
            anchor = active.nth(0).locator('a')
            if anchor.count() != 1:
                raise LiteratureError('我的求助 HTML 当前页链接无法核验。')
            _, number, per_page = self._page_href(self._attribute(anchor.first, 'href'))
            if anchor.first.inner_text(timeout=self._timeout_ms()).strip() != str(number):
                raise LiteratureError('我的求助当前页号与链接不一致。')
            current_query = parse_qs(urlsplit(self.page.url).query)
            if int(current_query.get('page', [1])[0]) != number:
                raise LiteratureError('我的求助实际页面与当前页标记不一致。')
            if current_query.get('per-page', [per_page]) != [per_page]:
                raise LiteratureError('我的求助实际每页条数与当前页链接不一致，未合并或完成扫描。')
            next_item, last_item = next_items.nth(0), last_items.nth(0)
            next_disabled = 'disabled' in (self._attribute(next_item, 'class') or '').split()
            last_disabled = 'disabled' in (self._attribute(last_item, 'class') or '').split()
            next_links, last_links = next_item.locator('a'), last_item.locator('a')
            if next_disabled:
                if next_links.count() or last_links.count() or not last_disabled:
                    raise LiteratureError('我的求助末页禁用状态无法核验，禁止新发。')
                return {'kind': 'html', 'complete': True, 'page': number, 'last_page': number, 'next_href': None}
            if next_links.count() != 1 or last_links.count() != 1 or last_disabled:
                raise LiteratureError('我的求助 HTML 下页或尾页链接无法核验。')
            if next_links.first.inner_text(timeout=self._timeout_ms()).strip() != '下页' or last_links.first.inner_text(timeout=self._timeout_ms()).strip() != '尾页':
                raise LiteratureError('我的求助 HTML 翻页标签发生变化，禁止猜测。')
            next_href, next_number, next_size = self._page_href(self._attribute(next_links.first, 'href'))
            _, last_number, last_size = self._page_href(self._attribute(last_links.first, 'href'))
            if next_number != number+1 or last_number < next_number or next_size != per_page or last_size != per_page:
                raise LiteratureError('我的求助分页不连续或每页条数发生变化，禁止新发。')
            return {'kind': 'html', 'complete': False, 'page': number, 'last_page': last_number, 'next_href': next_href}
        if layui.count():
            controls = self.page.locator('.layui-laypage-next')
            if layui.count() != 1 or controls.count() != 1:
                return {'kind': 'unknown', 'complete': False, 'next_href': None, 'last_page': None}
            control = controls.nth(0)
            disabled = ('layui-disabled' in (self._attribute(control, 'class') or '').split()
                or self._attribute(control, 'aria-disabled') == 'true' or self._attribute(control, 'disabled') is not None)
            if disabled:
                return {'kind': 'layui', 'complete': True, 'next_href': None, 'last_page': None}
            href = self._attribute(control, 'href') or ''
            if not href or href.lower().startswith('javascript:'):
                # 未确认可持久恢复的页面地址，不能据此宣告完整或新发。
                return {'kind': 'unknown', 'complete': False, 'next_href': None, 'last_page': None}
            relative, _, _ = self._page_href(href)
            return {'kind': 'layui', 'complete': False, 'next_href': relative, 'last_page': None}
        unknown = self.page.locator('[aria-label="分页"], [aria-label="pagination"]').count()
        return {'kind': 'unknown' if unknown else 'single', 'complete': not unknown, 'next_href': None, 'last_page': 1}

    @staticmethod
    def _head_signature(records, pager):
        value = {'records': [(item.id, item.title) for item in records], 'kind': pager['kind'], 'last_page': pager.get('last_page')}
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _merge_records(records, current):
        for item in current:
            if (not isinstance(item.id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', item.id)
                    or not isinstance(item.title, str) or not 1 <= len(item.title) <= 1000
                    or not isinstance(item.status, str) or len(item.status) > 200):
                raise LiteratureError('我的求助扫描记录身份无法核验。')
            if item.id in records and normalize_title(records[item.id].title) != normalize_title(item.title):
                raise LiteratureError('我的求助同一编号对应不同题名，禁止新发。')
            records[item.id] = item
        if len(records) > 20000:
            raise LiteratureError('我的求助记录超过可核验扫描上限，禁止新发。')

    def _save_scan(self, scan, records):
        scan['records'] = [{'id': item.id, 'title': item.title, 'status': item.status} for item in records.values()]
        self.store.save_scan(self.account['account_ref'], scan)

    def _head(self):
        self._scan_proof = None
        self._goto(MY_ASSISTS, '/my/assist-my')
        landing = site_url(self.page.url, '/my/assist-my')
        query = parse_qs(landing.query, keep_blank_values=True)
        if (set(query)-{'page', 'per-page'} or query.get('page', ['1']) != ['1']
                or ('per-page' in query and (len(query['per-page']) != 1
                    or not re.fullmatch(r'[1-9][0-9]{0,2}', query['per-page'][0])))):
            raise LiteratureError('我的求助头页没有实际落在第 1 页，禁止宣告完整或新发。')
        self._clear_notification()
        current, pager = self._records(), self._pagination()
        return current, pager, self._head_signature(current, pager)

    def _new_scan(self, current, pager, signature):
        records = {}
        self._merge_records(records, current)
        scan = {'started_at': self.store.now(), 'head_signature': signature,
            'phase': 'complete' if pager['complete'] else 'scanning', 'next_href': pager.get('next_href'),
            'visited': ['/my/assist-my'], 'kind': pager['kind']}
        self._save_scan(scan, records)
        return scan, records

    def list_requests(self):
        """每片首尾核对头页，并从已保存 href 续查。"""
        current, pager, signature = self._head()
        if pager['kind'] == 'unknown':
            return Listing(current, False)
        scan = self.store.read_scan(self.account['account_ref'])
        if scan is None or scan.get('head_signature') != signature:
            scan, records = self._new_scan(current, pager, signature)
        else:
            try:
                if scan.get('phase') not in {'scanning', 'verify_head', 'complete'} or not isinstance(scan.get('visited'), list) or len(scan['visited']) > 1000:
                    raise ValueError()
                records = {}
                self._merge_records(records, [RequestRecord(**value) for value in scan['records']])
                self._merge_records(records, current)
            except (ValueError, KeyError, TypeError):
                raise LiteratureError('原账号列表游标结构无法核验，禁止新发。') from None
            if scan['phase'] in {'verify_head', 'complete'}:
                scan['phase'] = 'complete'
                self._save_scan(scan, records)
                self._scan_proof = signature
                return Listing(list(records.values()), True)
        if scan['phase'] == 'complete':
            self._scan_proof = signature
            return Listing(list(records.values()), True)
        # 小片持续推进，防止大量历史记录让每轮都停在前几页。
        for _ in range(8):
            self.guard()
            cursor = scan.get('next_href')
            if not isinstance(cursor, str):
                raise LiteratureError('原账号的下一页游标缺失，禁止新发。')
            relative, expected_page, expected_size = self._page_href(cursor)
            if relative in scan['visited'] or len(scan['visited']) >= 1000:
                raise LiteratureError('我的求助分页发生循环或超出扫描上限，禁止新发。')
            self._goto(urljoin(ORIGIN, relative), '/my/assist-my')
            _, actual_page, actual_size = self._page_href(self.page.url)
            if (actual_page, actual_size) != (expected_page, expected_size):
                raise LiteratureError('我的求助分页落地页号或条数与保存游标不一致，未合并或完成扫描。')
            self._clear_notification()
            step = self._pagination()
            if step['kind'] != scan['kind'] or step['kind'] == 'unknown':
                raise LiteratureError('我的求助分页结构在扫描中变化，禁止新发。')
            if step.get('page', expected_page) != expected_page:
                raise LiteratureError('我的求助分页标记与保存游标不一致，未合并扫描记录。')
            rows = self._records()
            self._merge_records(records, rows)
            scan['visited'].append(relative)
            scan['next_href'] = step.get('next_href')
            scan['phase'] = 'verify_head' if step['complete'] else 'scanning'
            self._save_scan(scan, records)
            if step['complete']:
                latest, latest_pager, latest_signature = self._head()
                if latest_signature != signature:
                    self._new_scan(latest, latest_pager, latest_signature)
                    return Listing(latest, False, latest_pager['kind'] != 'unknown')
                self._merge_records(records, latest)
                scan['phase'] = 'complete'
                self._save_scan(scan, records)
                self._scan_proof = signature
                return Listing(list(records.values()), True)
        latest, latest_pager, latest_signature = self._head()
        if latest_signature != signature:
            self._new_scan(latest, latest_pager, latest_signature)
            return Listing(latest, False, latest_pager['kind'] != 'unknown')
        self._merge_records(records, latest)
        self._save_scan(scan, records)
        return Listing(list(records.values()), False, True)

    def _confirm_scan_before_publish(self):
        if getattr(self.store, 'job_id', None):
            baseline = getattr(self, '_task_baseline', None)
            if not isinstance(baseline, dict):
                raise LiteratureError('本次任务缺少发布前头页记录，未发布。')
            current, _, signature = self._head()
            if signature != baseline['signature'] or [item.id for item in current] != baseline['ids']:
                raise LiteratureError('本次发布前账号头页变化，尚未点击发布，将重新核对。', 'waiting')
            return
        scan = self.store.read_scan(self.account['account_ref'])
        if not scan or scan.get('phase') != 'complete' or self._scan_proof != scan.get('head_signature'):
            raise LiteratureError('尚无原账号列表的完整扫描凭据，未发布。')
        current, pager, signature = self._head()
        if pager['kind'] == 'unknown' or signature != scan['head_signature']:
            self._new_scan(current, pager, signature)
            raise LiteratureError('发布前原账号头页发生变化，已保存新游标并停止发布，下一轮继续核对。', 'waiting')

    def prepare_publish(self):
        """仅记录本次提交前的头页；旧同题、旧分页和旧扫描游标不作为新取文准入条件。"""
        current, _, signature = self._head()
        self._task_baseline = {'ids': [item.id for item in current], 'signature': signature}
        return dict(self._task_baseline)

    def recover_publish(self, baseline, title, doi, intent_at):
        """只恢复本次意图以后出现的唯一正文求助，不采用历史同题记录，不重发未知提交。"""
        ids = baseline.get('ids')
        if (not isinstance(ids, list) or len(ids) > 1000
                or any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', value) for value in ids)
                or not isinstance(intent_at, (int, float)) or not math.isfinite(intent_at)):
            raise LiteratureError('本次发布前记录无法核验，未重发。')
        current, _, _ = self._head()
        possible = [item for item in current if item.id not in ids
                    and normalize_title(item.title) == normalize_title(title)]
        matches = []
        for item in possible:
            self._goto(ORIGIN+'/assist/detail?'+urlencode({'id': item.id}), '/assist/detail')
            self._clear_notification()
            body = self.page.locator('body').inner_text(timeout=self._timeout_ms())
            if self._supplement_request(body):
                continue
            created = re.search(r'求助人[\s\S]{0,1000}?在\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*发布', body)
            if not created:
                continue
            try:
                published_at = datetime.strptime(created[1], '%Y-%m-%d %H:%M:%S').replace(
                    tzinfo=timezone(timedelta(hours=8))).timestamp()
            except ValueError:
                continue
            # 页面时间精确到秒，容纳最多两秒时钟/舍入偏差；更早的同题旧帖不能作为提交结果。
            if math.floor(intent_at)-2 <= published_at <= self.store.now()+2:
                if doi and detail_publication_dois(body) != extract_dois(clean_doi(doi)):
                    continue
                if self.identity and not self.identity['doi'] and not platform_identity_matches(body, self.identity):
                    continue
                self._verify_detail(item.id, title, doi)
                matches.append(item)
        if len(matches) > 1:
            raise LiteratureError('本次发布后出现多个同题正文求助，未盲目绑定或重发。')
        return matches[0] if matches else None

    @staticmethod
    def _supplement_request(body):
        field = re.search(r'(?:^|\n)\s*标题\s*[:：]?\s*(.*?)\n\s*(?:网址|DOI|其它|其他)\s*[:：]?', body, re.S)
        return bool(field and re.search(r'[\[【]\s*求助补充材料\s*[\]】]', field[1]))

    def _verify_detail(self, identifier, title, doi):
        if request_id(self.page.url) != identifier:
            raise LiteratureError('求助详情页未保持原请求 ID。')
        body = self.page.locator('body').inner_text(timeout=self._timeout_ms())
        if normalize_title(title) not in normalize_title(body):
            raise LiteratureError('求助详情页无法核验目标论文题名。')
        if doi and (extract_dois(clean_doi(doi)) != detail_publication_dois(body)):
            raise LiteratureError('求助详情页本文 DOI 字段未能唯一确认完整标识。')
        if self.identity and not self.identity['doi'] and not platform_identity_matches(body, self.identity):
            raise LiteratureError('科研通详情缺少匹配的明确期刊与年份字段，无法区分同题论文。')

    def _wait_for_doi_retry(self):
        # 同一账号的两次正常提取留出间隔，等待中仍核对租约、取消和安全状态。
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            self._security()
            self._pause(200, deadline)

    def publish(self, title, doi, before_submit):
        doi = clean_doi(doi)
        self._confirm_scan_before_publish()
        queries = [title] + ([doi] if doi and doi != title else [])
        for index, value in enumerate(queries):
            if index:
                self._wait_for_doi_retry()
            # 重新打开表单，清除失败提取的弹窗；回退只查询已绑定的 DOI。
            self._goto(CREATE, '/assist/create')
            self._clear_notification()
            form = self.page.locator('#assist-create-form')
            query = self.page.locator('#onekey')
            search = self._visible(self.page.locator('.onekey-search'))
            if form.count() != 1 or not query.is_visible() or len(search) != 1:
                raise LiteratureError('科研通智能提取表单无法核验，未发布。')
            self._fill(query, value)
            self._click(search[0])
            def extraction():
                layer, message, labels = self._layer()
                if all(label in labels for label in EXTRACTION_LABELS):
                    return message
                if message == '对不起，您的查询频率太快，请休息一下' and labels == ['确定']:
                    raise LiteratureError('科研通查询频率受限，已暂停本轮并等待稍后重试。', 'waiting')
                if layer and message and labels and not any(word in message for word in LOADING):
                    raise LiteratureError('智能提取返回未知提示，未发布。')
                return None
            message = self._wait(extraction, seconds=20, message='智能提取未返回可核验结果，未发布。')
            try:
                actual_title, actual_doi = extracted_identity(message)
            except LiteratureError:
                if value == queries[-1]:
                    raise
                continue
            if normalize_title(actual_title) == normalize_title(title) and (not doi or actual_doi == doi):
                break
        else:
            raise LiteratureError('智能提取结果与所选论文身份不一致，未发布。')
        if self.identity and not self.identity['doi'] and not platform_identity_matches(message, self.identity):
            raise LiteratureError('智能提取未能证实所选期刊与年份，未发布同题无 DOI 求助。')
        # 只有此回调完成 fsync，才允许一次性点击站点提供的直接发布。
        before_submit()
        self._click_layer(EXTRACTION_LABELS[0])
        def published():
            if urlsplit(self.page.url).path.rstrip('/') == '/assist/detail':
                identifier = request_id(self.page.url)
                self._verify_detail(identifier, title, doi)
                return RequestRecord(identifier, title, '已发布')
            layer, message, labels = self._layer()
            if all(label in labels for label in EXTRACTION_LABELS):
                return None
            if '求助发布成功' in message and labels == ['查看求助详情']:
                self._click_layer('查看求助详情')
                return None
            if layer and message and labels and not any(word in message for word in LOADING):
                raise LiteratureError('发布后站点要求额外确认，已保存发布意图并停止；请核对原求助。')
            return None
        return self._wait(published, seconds=20, message='发布结果未能核验，已保存发布意图，下一轮只恢复原求助。', state='waiting')

    def source(self, identifier, title, doi):
        self._goto(ORIGIN+'/assist/detail?'+urlencode({'id': identifier}), '/assist/detail')
        self._clear_notification()
        self._verify_detail(identifier, title, doi)
        body = self.page.locator('body').inner_text(timeout=self._timeout_ms())
        evidence = hashlib.sha256(body.encode('utf-8')).hexdigest()
        if self._supplement_request(body):
            return SourceState('supplement_only', evidence_sha256=evidence)
        badges = self.page.locator('.assist-list-index > .fly-panel > .assist-badge')
        if badges.count() != 1:
            raise LiteratureError('求助详情的业务状态无法唯一核验。')
        badge = badges.first.inner_text(timeout=self._timeout_ms()).strip()
        if '文件已从服务器自动删除' in body and '无法下载' in body and badge == '已完结':
            return SourceState('expired', evidence_sha256=evidence)
        attachment = self.attachment(identifier, title, doi)
        if attachment is not None:
            return SourceState('available', attachment, evidence)
        if badge == '已完结':
            return SourceState('finished_without_file', evidence_sha256=evidence)
        if badge in {'待应助', '求助中', '等待应助', '待采纳'}:
            return SourceState('awaiting', evidence_sha256=evidence)
        raise LiteratureError('求助详情返回未知业务状态，未冒充等待全文。')

    def attachment(self, identifier, title, doi):
        target = ORIGIN+'/assist/detail?'+urlencode({'id': identifier})
        self._goto(target, '/assist/detail')
        self._clear_notification()
        self._verify_detail(identifier, title, doi)
        hidden = self.page.locator('input.assist-file-id')
        if hidden.count() > 1:
            raise LiteratureError('原求助存在多个附件，无法自动确定正确 PDF，未盲选。')
        if hidden.count() == 1:
            field = hidden.first
            file_id = (field.get_attribute('value', timeout=self._timeout_ms()) or '').strip()
            item = field.locator('xpath=ancestor::li[1]')
            if not item.count():
                item = field.locator('..')
            links = item.locator('a[title="点击下载"][href*="/assist/download?id="]')
            buttons = item.locator('button.btn-handle-file[data-type="accept"]')
            if buttons.count() > 1:
                raise LiteratureError('应助附件采纳控件无法唯一识别。')
            needs_accept = buttons.count() == 1
        else:
            links = self.page.locator('a[title="点击下载"][href*="/assist/download?id="]')
            if not links.count():
                return None
            if not self.page.locator('.assist-badge-finished').count():
                raise LiteratureError('应助下载链接缺少对应文件 ID，未下载。')
            parsed = site_url(links.first.get_attribute('href', timeout=self._timeout_ms()), '/assist/download')
            identifiers = parse_qs(parsed.query).get('id', [])
            file_id = identifiers[0] if len(identifiers) == 1 else ''
            needs_accept = False
        if links.count() != 1 or not re.fullmatch(r'[A-Za-z0-9]{1,64}', file_id):
            raise LiteratureError('应助 PDF 与文件 ID 无法唯一对应，未下载。')
        anchor = links.first
        site_url(anchor.get_attribute('href', timeout=self._timeout_ms()), '/assist/download', file_id)
        name = ' '.join(anchor.inner_text(timeout=self._timeout_ms()).split())
        if not name.lower().endswith('.pdf'):
            raise LiteratureError('唯一应助附件未标明 PDF，未下载。')
        return Attachment(file_id, name, needs_accept)

    def download(self, attachment, target):
        target = self.store.path(Path(target).relative_to(self.root))
        self.store._directory(target.parent)
        site_url(ORIGIN+'/assist/download?'+urlencode({'id': attachment.id}), '/assist/download', attachment.id)
        self._security()
        self.guard(force=True)
        browser_cdp = page_cdp = None
        begun, progress, downloads = {}, {}, []
        successful = False
        continued = False
        highspeed_selected = False
        highspeed_confirmed = False
        context_id = None
        download_root = None
        def begin(event):
            if event.get('frameId') not in frame_ids:
                return
            guid = event.get('guid')
            if isinstance(guid, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', guid):
                begun[guid] = {'url': event.get('url')}
        def advance(event):
            if event.get('guid') in begun:
                progress[event['guid']] = {key: event.get(key) for key in ('state', 'receivedBytes', 'totalBytes')}
        def delivered(download):
            downloads.append(download)
        try:
            # 仅使用公开 CDPSession；正常页面发起下载，不复制 Cookie 或调用站点私有接口。
            browser_cdp = self.browser.new_browser_cdp_session()
            page_cdp = self.context.new_cdp_session(self.page)
            info = page_cdp.send('Target.getTargetInfo')
            context_id = info.get('targetInfo', {}).get('browserContextId')
            tree = page_cdp.send('Page.getFrameTree')
            frame_ids = set()
            def collect(node):
                if isinstance(node, dict):
                    identifier = node.get('frame', {}).get('id')
                    if isinstance(identifier, str):
                        frame_ids.add(identifier)
                    for child in node.get('childFrames', []):
                        collect(child)
            collect(tree.get('frameTree'))
            if not isinstance(context_id, str) or not context_id or not frame_ids:
                raise LiteratureError('无法限定下载所属浏览器会话与页面，未下载。')
            download_root = self.store.path((self.runtime/'downloads').relative_to(self.root))
            self.store._directory(download_root)
            browser_cdp.on('Browser.downloadWillBegin', begin)
            browser_cdp.on('Browser.downloadProgress', advance)
            self.page.on('download', delivered)
            browser_cdp.send('Browser.setDownloadBehavior', {'behavior': 'allowAndName',
                'browserContextId': context_id, 'downloadPath': str(download_root), 'eventsEnabled': True})
            # 导航和事件等待均受当前任务剩余时间约束。
            def remaining(maximum):
                self.guard()
                getter = getattr(self.guard, 'remaining_seconds', None)
                value = getter() if callable(getter) else None
                return min(maximum, value) if isinstance(value, (int, float)) and math.isfinite(value) else maximum
            until = time.monotonic()+remaining(900.0)
            try:
                self.page.goto(ORIGIN+'/assist/download?'+urlencode({'id': attachment.id}),
                    wait_until='domcontentloaded', timeout=max(1, int(1000*remaining(20.0))))
            except Exception:
                # 直接文件响应会结束导航；仍须收到该页面的下载事件及 completed 事件。
                self.guard()
            while True:
                self._security()
                if not begun and not downloads and not highspeed_selected:
                    controls = self._visible(self.page.locator('#download-highspeed-direct'))
                    if not controls:
                        controls = self._visible(self.page.locator('#download-switch-vip'))
                    if controls:
                        site_url(self.page.url, '/assist/download', attachment.id)
                        if len(controls) != 1 or '高速通道' not in controls[0].inner_text(timeout=self._timeout_ms()):
                            raise LiteratureError('高速下载入口无法唯一核验，未点击。')
                        if controls[0].is_enabled(timeout=self._timeout_ms()):
                            highspeed_selected = True
                            self._click(controls[0])
                if highspeed_selected and not highspeed_confirmed:
                    layer, message, labels = self._layer()
                    if layer and labels == ['确认使用高速通道', '取消'] and '高速通道积分规则' in message:
                        highspeed_confirmed = True
                        self._click_layer('确认使用高速通道')
                    elif layer and labels and not any(word in message for word in LOADING):
                        raise LiteratureError('高速下载确认状态无法核验，已停止本轮下载。')
                if len(begun) > 1 or len(downloads) > 1:
                    raise LiteratureError('页面发起多个下载，无法唯一对应应助 PDF，已取消。')
                if begun and downloads:
                    guid = next(iter(begun))
                    if not isinstance(begun[guid]['url'], str) or begun[guid]['url'] != downloads[0].url:
                        raise LiteratureError('浏览器下载事件与文件进度无法唯一对应。')
                    current = progress.get(guid, {})
                    for key in ('receivedBytes', 'totalBytes'):
                        value = current.get(key, 0)
                        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                            raise LiteratureError('浏览器下载大小无法核验，已取消。')
                        if value > MAX_PDF_BYTES:
                            raise LiteratureError('应助 PDF 超过允许大小，已取消下载。')
                    if current.get('state') == 'canceled':
                        raise LiteratureError('浏览器下载已取消，将保留原求助稍后重试。', 'waiting')
                    if current.get('state') == 'completed':
                        source = self.store.path((download_root/guid).relative_to(self.root))
                        if source.is_file():
                            if source.stat().st_size > MAX_PDF_BYTES:
                                raise LiteratureError('应助 PDF 超过允许大小，未返回下载。')
                            with source.open('rb') as stream:
                                data = stream.read(MAX_PDF_BYTES+1)
                            if len(data) > MAX_PDF_BYTES:
                                raise LiteratureError('应助 PDF 超过允许大小，未返回下载。')
                            self.guard()
                            self.store._atomic(target, data)
                            self.guard()
                            successful = True
                            return data
                if time.monotonic() >= until:
                    raise LiteratureError('本轮站内下载未完成，已取消并保留原求助。', 'waiting')
                if not begun and not downloads and not continued and not highspeed_selected:
                    controls = self._visible(self.page.locator('button#download-continue'))
                    if controls:
                        site_url(self.page.url, '/assist/download', attachment.id)
                        if len(controls) != 1 or controls[0].inner_text(timeout=self._timeout_ms()).strip() != '继续当前下载':
                            raise LiteratureError('普通下载确认控件无法唯一核验，未点击。')
                        if controls[0].is_enabled(timeout=self._timeout_ms()):
                            continued = True
                            self._click(controls[0])
                self._pause(50, until)
        except (LiteratureError, LeaseLost, SliceExpired):
            raise
        except Exception:
            raise LiteratureError('站内下载暂未完成，将保留原求助继续检查。', 'waiting') from None
        finally:
            if browser_cdp is not None:
                for guid in begun:
                    if progress.get(guid, {}).get('state') != 'completed':
                        try:
                            browser_cdp.send('Browser.cancelDownload', {'guid': guid, 'browserContextId': context_id})
                        except Exception:
                            pass
                for name, callback in (('Browser.downloadWillBegin', begin), ('Browser.downloadProgress', advance)):
                    try:
                        browser_cdp.remove_listener(name, callback)
                    except Exception:
                        pass
            try:
                self.page.remove_listener('download', delivered)
            except Exception:
                pass
            for session in (page_cdp, browser_cdp):
                if session is not None:
                    try:
                        session.detach()
                    except Exception:
                        pass
            if download_root is not None:
                for guid in begun:
                    for name in (guid, guid+'.crdownload'):
                        try:
                            self.store.path((download_root/name).relative_to(self.root)).unlink(missing_ok=True)
                        except OSError:
                            pass
            if not successful:
                target.unlink(missing_ok=True)

    def accept(self, identifier, title, attachment):
        if not re.fullmatch(r'[A-Za-z0-9]{1,64}', attachment.id):
            raise LiteratureError('采纳文件 ID 格式无法核验。')
        self._goto(ORIGIN+'/assist/detail?'+urlencode({'id': identifier}), '/assist/detail')
        self._clear_notification()
        self._verify_detail(identifier, title, None)
        if self.page.locator('.assist-badge-finished').count():
            return
        hidden = self.page.locator(f'input.assist-file-id[value="{attachment.id}"]')
        if hidden.count() != 1:
            raise LiteratureError('已核验 PDF 对应的采纳文件 ID 不存在。')
        item = hidden.locator('xpath=ancestor::li[1]')
        if not item.count():
            item = hidden.locator('..')
        buttons = item.locator('button.btn-handle-file[data-type="accept"]')
        if buttons.count() != 1:
            raise LiteratureError('已核验 PDF 对应的采纳按钮无法唯一识别。')
        self._wait(lambda: buttons.first.evaluate('''button => {
            const q = window.layui && window.layui.jquery;
            const events = q && q._data && q._data(button, 'events');
            return !!(events && events.click && events.click.length);
        }''', timeout=self._timeout_ms()), seconds=8, message='科研通采纳按钮事件尚未就绪，未重复点击。')
        self._click(buttons.first)
        def confirmation():
            _, message, labels = self._layer()
            if '确认接受应助吗' in message and '确定' in labels:
                return True
            if message and labels and not any(word in message for word in LOADING):
                raise LiteratureError('采纳确认弹窗与已知流程不符，未确认。')
            return False
        self._wait(confirmation, seconds=10, message='采纳确认弹窗未能核验，未重复点击。')
        notes = self._visible(self.page.locator('#handle-note'))
        if len(notes) == 1:
            self._fill(notes[0], '感谢')
        self._click_layer('确定')
        extra_sent = [False]
        def finished():
            if self.page.locator('.assist-badge-finished').count():
                return True
            _, message, labels = self._layer()
            if '确认接受应助吗' in message:
                return False
            if labels == ['确定'] and any(word in message for word in ('采纳成功', '接受成功', '操作成功')):
                self._click_layer('确定')
                return False
            if '确认提交' in labels and any(word in message for word in ('采纳', '接受应助')) and not extra_sent[0]:
                extra_sent[0] = True
                self._click_layer('确认提交')
                return False
            if message and labels and not any(word in message for word in LOADING):
                raise LiteratureError('采纳返回未知提示，已保留核验全文并停止。')
            return False
        self._wait(finished, seconds=18, message='已核验全文，但采纳完成状态尚未确认，将从原请求继续核验。', state='waiting')
