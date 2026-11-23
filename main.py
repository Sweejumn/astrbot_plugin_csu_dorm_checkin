from __future__ import annotations

import asyncio
import json
import re
from contextlib import asynccontextmanager, suppress
from datetime import datetime, time
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, StarTools, register
from playwright.async_api import Error as BrowserError, async_playwright

NAME = "astrbot_plugin_csu_dorm_checkin"
ORIGIN = "https://zhxg.csu.edu.cn"
APP_PATH = "/znzhxgpt_h5/"
ROUTE = "/ywViews/qxjlfx/dk/index"
CHECKIN_URL = f"{ORIGIN}{APP_PATH}#{ROUTE}"
CAS = "https://ca.csu.edu.cn/authserver/login"
CAS_URL = f"{CAS}?{urlencode({'service': f'{ORIGIN}/fdcwonsun/caslogin_h5.jsp'})}"
TITLE = "校内住宿打卡"
READY = "立即打卡"
DONE = "今日已打卡"
EXEMPT = "无需打卡"
ZONE = ZoneInfo("Asia/Shanghai")
START, DEFAULT, END = time(20), time(20, 10), time(23, 30)
USAGE = (
    "/住宿打卡 登录：在机器人电脑上打开学校登录页\n"
    "/住宿打卡 扫码：查看手机微信扫码登录说明\n"
    "/住宿打卡 检查：检查会话、页面和真实定位\n"
    "/住宿打卡 打卡：在有效时间内使用真实定位提交一次\n"
    "/住宿打卡 开启 20:10：你与机器人电脑在同一打卡地点时启用定时打卡\n"
    "/住宿打卡 关闭：暂停定时打卡\n"
    "/住宿打卡 状态：查看定时设置和上次结果"
)


def task_title(url: str) -> str | None:
    parsed = urlsplit(url)
    route, _, query = parsed.fragment.partition("?")
    if (
        f"{parsed.scheme}://{parsed.netloc}" != ORIGIN
        or parsed.path != APP_PATH
        or route != ROUTE
    ):
        return None
    name = parse_qs(query).get("dkbc", [""])[0]
    return unquote(name)


@register(NAME, "local", "中南大学校内住宿打卡", "0.2.1")
class DormCheckin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir(NAME)
        self.profile = self.data_dir / "edge"
        self.state_path = self.data_dir / "state.json"
        self.state = (
            json.loads(self.state_path.read_text(encoding="utf-8"))
            if self.state_path.exists()
            else {}
        )
        self.browser_lock = asyncio.Lock()
        self.scheduler: asyncio.Task | None = None
        self.login_task: asyncio.Task | None = None

    def save(self, **changes: str) -> None:
        state = {**self.state, **changes}
        temp = self.state_path.with_suffix(".tmp")
        temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        temp.replace(self.state_path)
        self.state = state

    async def initialize(self) -> None:
        self.scheduler = asyncio.create_task(self.schedule())

    async def terminate(self) -> None:
        tasks = [task for task in (self.scheduler, self.login_task) if task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @asynccontextmanager
    async def edge(self, *, headless: bool):
        async with self.browser_lock, async_playwright() as pw:
            mobile = pw.devices["Pixel 7"].copy()
            mobile.pop("default_browser_type", None)
            browser = await pw.chromium.launch_persistent_context(
                self.profile,
                channel="msedge",
                headless=headless,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                args=["--no-proxy-server"],
                **mobile,
            )
            try:
                # Chromium uses its normal geolocation provider. No coordinates are injected.
                await browser.grant_permissions(["geolocation"], origin=ORIGIN)
                yield browser.pages[0] if browser.pages else await browser.new_page()
            finally:
                with suppress(BrowserError):
                    await browser.close()

    async def notify(self, target: str, message: str) -> None:
        try:
            if not await self.context.send_message(target, MessageChain().message(message)):
                logger.warning("[%s] QQ 私聊发送失败", NAME)
        except Exception as exc:
            logger.warning("[%s] QQ 私聊发送异常 (%s)", NAME, type(exc).__name__)

    async def login(self, target: str) -> None:
        result = "登录窗口已关闭或超时，可用 /住宿打卡 检查 验证会话。"
        try:
            async with self.edge(headless=False) as page:
                await page.goto(CHECKIN_URL, wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_url(
                    lambda url: task_title(url) is not None and "?" in urlsplit(url).fragment,
                    timeout=300000,
                )
                await page.locator(".punch-btn-text").wait_for(timeout=15000)
                result = "Edge 会话已保存。请用 /住宿打卡 检查 验证登录和真实定位。"
        except BrowserError:
            pass
        await self.notify(target, result)

    async def school_qr(self, page) -> bytes:
        await page.goto(CHECKIN_URL, wait_until="domcontentloaded", timeout=25000)
        await page.set_viewport_size({"width": 1100, "height": 850})

        async def desktop_cas(route):
            headers = dict(route.request.headers)
            headers["user-agent"] = (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            )
            await route.continue_(headers=headers)

        cas_only = re.compile(r"^https://ca\.csu\.edu\.cn/authserver/")
        await page.route(cas_only, desktop_cas)
        await page.goto(CAS_URL, wait_until="domcontentloaded", timeout=25000)
        iframe = await page.locator("#wxiframe").element_handle(timeout=12000)
        wx = await iframe.content_frame()
        if wx is None:
            raise BrowserError("WeChat login frame missing")
        await wx.wait_for_url(
            lambda url: urlsplit(url).scheme == "https"
            and urlsplit(url).hostname == "open.weixin.qq.com",
            timeout=15000,
        )
        await wx.wait_for_function(
            """() => Array.from(document.querySelectorAll("img.js_qrcode_img"))
                .some(img => img.naturalWidth > 0 && img.getClientRects().length)""",
            timeout=15000,
        )
        return await wx.locator("img.js_qrcode_img:visible").first.screenshot(timeout=5000)

    async def qr_login(self, event: AstrMessageEvent) -> None:
        try:
            async with self.edge(headless=True) as page:
                qr = await self.school_qr(page)
                message = MessageChain().message(
                    "这是学校统一认证的微信登录码。保存图片后在微信扫一扫里从相册选图，核对学校名称后确认。"
                )
                message.chain.append(Image.fromBytes(qr))
                await event.send(message)
                await page.wait_for_url(
                    lambda url: url.startswith(f"{ORIGIN}{APP_PATH}"), timeout=180000
                )
                await page.wait_for_function(
                    "() => localStorage.getItem('H5_AUTHED') === '1'", timeout=20000
                )
            result = "学校登录会话已保存。请用 /住宿打卡 检查 验证登录和真实定位。"
        except Exception as exc:
            logger.info("[%s] 扫码流程未完成 (%s)", NAME, type(exc).__name__)
            result = "学校扫码登录未完成或已超时，可重新获取登录码。"
        await self.notify(event.unified_msg_origin, result)

    async def check(self, *, submit: bool, manual: bool = False) -> str:
        if self.browser_lock.locked() or self.login_task and not self.login_task.done():
            return "Edge 会话正在使用，请稍后检查。"
        if not self.profile.exists():
            return "还没有本机登录会话，请用 /住宿打卡 扫码 获取登录说明。"
        target = self.state.get("target") if submit else None
        clicked = False
        try:
            async with self.edge(headless=True) as page:
                await page.goto(CHECKIN_URL, wait_until="domcontentloaded", timeout=25000)
                await page.wait_for_url(
                    lambda url: (
                        task_title(url) is not None and "?" in urlsplit(url).fragment
                    ) or (
                        task_title(url) is None and url.startswith(f"{ORIGIN}{APP_PATH}#/pages/")
                    ),
                    timeout=18000,
                )
                route = urlsplit(page.url).fragment.partition("?")[0]
                if route.startswith("/pages/login/"):
                    return "学校登录会话已失效，请重新登录。"
                if task_title(page.url) != TITLE:
                    return "学校当前未开放校内住宿打卡，请在手机上确认。"

                label = page.locator(".punch-area .punch-btn-text")
                await label.wait_for(timeout=12000)
                status = (await label.inner_text()).strip()
                if submit and status in (DONE, EXEMPT):
                    return f"学校页面显示：{status}。"
                if submit and status != READY:
                    return "学校页面暂不允许打卡，请在手机上确认。"

                # Record only the browser error code, never the position.
                location_error = await page.evaluate(
                    """() => new Promise(resolve => {
                        if (!navigator.geolocation) return resolve(-1);
                        navigator.geolocation.getCurrentPosition(
                            ({coords}) => resolve(Number.isFinite(coords.accuracy) ? 0 : -1),
                            error => resolve(error.code),
                            {enableHighAccuracy: true, timeout: 12000, maximumAge: 0}
                        );
                    })"""
                )
                if location_error:
                    reason = {1: "定位权限被拒绝", 2: "定位服务无法取得位置", 3: "定位超时"}.get(
                        location_error, "浏览器定位不可用"
                    )
                    logger.info("[%s] 本机定位失败，浏览器定位错误码 %s", NAME, location_error)
                    return f"学校登录会话有效，但本机{reason}。请用手机微信打开学校页面办理。"
                if not submit:
                    return "登录会话有效，本机浏览器可读取真实定位。检查未提交打卡。"
                if not manual and (not target or self.state.get("target") != target):
                    return "已取消本次定时打卡。"
                if not START <= datetime.now(ZONE).time() < END:
                    return "当前超出 20:00 至 23:30 的打卡时段，请在手机上确认。"
                if task_title(page.url) != TITLE:
                    return "学校页面已切换，请在手机上确认。"
                button = page.locator(
                    ".punch-area:not(.no_through):not(.punched) .punch-circle-inner"
                ).filter(has_text=READY)
                clicked = True
                await button.click(timeout=5000)
                await label.filter(has_text=re.compile(f"^{DONE}$")).wait_for(timeout=35000)
                return "打卡成功，学校页面显示「今日已打卡」。"
        except BrowserError as exc:
            logger.info("[%s] Edge 操作未完成 (%s)", NAME, type(exc).__name__)
            if clicked:
                return "提交结果待确认，请用手机查看学校页面。今天不会再自动提交。"
            return "未进入可操作的打卡页面，请在手机上确认。"

    async def schedule(self) -> None:
        while True:
            try:
                now = datetime.now(ZONE)
                today = now.date().isoformat()
                target = self.state.get("target")
                run_at = time.fromisoformat(self.state.get("run_at", "20:10"))
                if (
                    target
                    and self.state.get("last_day") != today
                    and run_at <= now.time() < END
                    and (now.hour, now.minute) == (run_at.hour, run_at.minute)
                ):
                    self.save(last_day=today, last_result="结果待确认")
                    result = await self.check(submit=True)
                    self.save(last_result=result)
                    await self.notify(target, f"中南大学住宿打卡｜{result}")
            except Exception as exc:
                logger.warning("[%s] 定时任务异常 (%s)", NAME, type(exc).__name__)
            await asyncio.sleep(10)

    @filter.command("住宿打卡")
    async def command(self, event: AstrMessageEvent):
        if not event.is_private_chat() or not event.is_admin():
            return
        parts = event.message_str.split()
        action, *params = parts[1:] or [""]
        if action.startswith("开启") and action != "开启":
            action, params = "开启", [action.removeprefix("开启"), *params]
        if action == "登录":
            if self.browser_lock.locked() or self.login_task and not self.login_task.done():
                yield event.plain_result("Edge 会话正在使用。")
                return
            self.login_task = asyncio.create_task(self.login(event.unified_msg_origin))
            yield event.plain_result("已请求在机器人电脑上打开 Edge。请在学校统一认证页面完成登录。")
        elif action == "扫码":
            if params != ["同意"]:
                yield event.plain_result(
                    "请先在学校登录页阅读《用户服务协议》和《隐私政策》。"
                    "学校提示认证会收集手机号用于身份核验和学工事务。"
                    f"阅读并同意后，发送 /住宿打卡 扫码 同意 获取微信登录码。\n{CHECKIN_URL}"
                )
                return
            if self.browser_lock.locked() or self.login_task and not self.login_task.done():
                yield event.plain_result("Edge 会话正在使用。")
                return
            self.login_task = asyncio.create_task(self.qr_login(event))
            yield event.plain_result("正在向学校统一认证申请微信登录码。")
        elif action == "打卡":
            yield event.plain_result(await self.check(submit=True, manual=True))
        elif action == "检查":
            yield event.plain_result(await self.check(submit=False))
        elif action == "开启":
            try:
                run_at = DEFAULT if not params else time.fromisoformat(params[0])
                if (
                    len(params) > 1 or run_at.tzinfo or run_at.second or run_at.microsecond
                    or not START <= run_at < END
                ):
                    raise ValueError
            except ValueError:
                yield event.plain_result("请输入 20:00 至 23:29 之间的时间，例如 /住宿打卡 开启 20:10。")
                return
            self.save(target=event.unified_msg_origin, run_at=run_at.strftime("%H:%M"))
            yield event.plain_result(f"已开启每天 {run_at:%H:%M} 的住宿打卡（北京时间）。")
        elif action == "关闭":
            self.save(target="")
            yield event.plain_result("定时打卡已关闭。")
        elif action == "状态":
            schedule = self.state.get("run_at", "20:10") if self.state.get("target") else "关闭"
            result = self.state.get("last_result", "尚无记录")
            day = self.state.get("last_day", "")
            yield event.plain_result(f"定时打卡｜{schedule}\n上次结果｜{day} {result}")
        else:
            yield event.plain_result(USAGE)
