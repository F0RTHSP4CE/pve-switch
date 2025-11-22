import asyncio
import logging
import os
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal, Optional

from aiogram import Bot, Dispatcher, Router, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, Filter
from aiogram.types import Message
from litestar import Controller, Litestar, get, post
from litestar.di import Provide
from litestar.status_codes import (
    HTTP_200_OK,
    HTTP_403_FORBIDDEN,
    HTTP_500_INTERNAL_SERVER_ERROR,
)
from proxmoxer import ProxmoxAPI, ResourceException

# -----------------------------------------------------------------------------
# 1. Configuration & Logging Setup
# -----------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("VMManager")


@dataclass
class Config:
    """Holds application configuration from environment variables."""

    proxmox_host: str
    proxmox_user: str
    proxmox_token_name: str
    proxmox_token_value: str
    proxmox_node: str
    vm_id_linux: int
    vm_id_win: int
    bot_token: str
    bot_chat_id: int  # The specific chat ID allowed to control VMs
    lock_file_path: str

    @classmethod
    def from_env(cls) -> "Config":
        try:
            return cls(
                proxmox_host=os.environ["PROXMOX_HOST"],
                proxmox_user=os.environ["PROXMOX_USER"],
                proxmox_token_name=os.environ["PROXMOX_TOKEN_NAME"],
                proxmox_token_value=os.environ["PROXMOX_TOKEN_VALUE"],
                proxmox_node=os.environ["PROXMOX_NODE_NAME"],
                vm_id_linux=int(os.environ["PROXMOX_LINUX_VM_ID"]),
                vm_id_win=int(os.environ["PROXMOX_WIN_VM_ID"]),
                bot_token=os.environ["BOT_TOKEN"],
                bot_chat_id=int(os.environ["BOT_CHAT_ID"]),
                lock_file_path=os.environ.get("LOCK_FILE_PATH", "lock.local"),
            )
        except KeyError as e:
            logger.critical(f"Missing environment variable: {e}")
            raise
        except ValueError as e:
            logger.critical(f"Invalid format for environment variable: {e}")
            raise


# -----------------------------------------------------------------------------
# 2. VM Controller Logic
# -----------------------------------------------------------------------------


class VMController:
    """
    Handles interaction with Proxmox API and manages the state of the switch.
    """

    def __init__(self, config: Config, bot: Bot):
        self.cfg = config
        self.bot = bot
        self.proxmox = ProxmoxAPI(
            config.proxmox_host,
            user=config.proxmox_user,
            token_name=config.proxmox_token_name,
            token_value=config.proxmox_token_value,
            verify_ssl=False,
        )
        self._op_lock = asyncio.Lock()  # Prevents concurrent operations
        self._manual_lock = self._load_lock_state()

    def _load_lock_state(self) -> bool:
        """Loads the lock state from the configured file path."""
        if os.path.exists(self.cfg.lock_file_path):
            try:
                with open(self.cfg.lock_file_path, "r") as f:
                    content = f.read().strip()
                    is_locked = content == "LOCKED"
                    if is_locked:
                        logger.info(
                            f"Restored LOCKED state from {self.cfg.lock_file_path}"
                        )
                    return is_locked
            except Exception as e:
                logger.error(f"Failed to load lock state: {e}")
        return False

    @property
    def is_locked(self) -> bool:
        return self._manual_lock

    def set_lock(self, locked: bool):
        self._manual_lock = locked
        try:
            with open(self.cfg.lock_file_path, "w") as f:
                f.write("LOCKED" if locked else "UNLOCKED")
        except Exception as e:
            logger.error(f"Failed to save lock state: {e}")

    def _get_node(self):
        return self.proxmox.nodes(self.cfg.proxmox_node)

    def get_vm_status(self, vmid: int) -> str:
        try:
            status_data = self._get_node().qemu(vmid).status.current.get()
            return status_data.get("status", "unknown")
        except Exception as e:
            logger.error(f"Failed to get status for VM {vmid}: {e}")
            return "error"

    def get_full_status(self) -> dict[str, str]:
        return {
            "linux": self.get_vm_status(self.cfg.vm_id_linux),
            "windows": self.get_vm_status(self.cfg.vm_id_win),
            "locked": str(self.is_locked),
        }

    async def _wait_for_shutdown(self, vmid: int, timeout: int = 180) -> bool:
        """Polls VM status until it is 'stopped' or timeout is reached."""
        for _ in range(timeout):
            if self.get_vm_status(vmid) == "stopped":
                return True
            await asyncio.sleep(1)
        return False

    async def perform_switch(
        self, target_os: Literal["linux", "windows"], quiet_on_skip: bool = False
    ) -> dict[str, Any]:
        """
        Main logic to switch VMs with real-time Telegram progress updates.
        """
        if self.is_locked:
            return {"status": "error", "message": "System is manually locked."}

        if self._op_lock.locked():
            return {
                "status": "error",
                "message": "An operation is already in progress.",
            }

        async with self._op_lock:
            target_id = (
                self.cfg.vm_id_linux if target_os == "linux" else self.cfg.vm_id_win
            )
            source_id = (
                self.cfg.vm_id_win if target_os == "linux" else self.cfg.vm_id_linux
            )

            source_name = "Windows" if target_os == "linux" else "Linux"
            target_name = "Linux" if target_os == "linux" else "Windows"

            # 1. Pre-flight check: Is target already running?
            try:
                if self.get_vm_status(target_id) == "running":
                    msg = f"ℹ️ <b>{target_name}</b> is already running. No action taken."
                    logger.info(f"Skipping switch: {target_name} is already running.")

                    if not quiet_on_skip:
                        # Notify user directly as we haven't started the progress message sequence
                        await self.bot.send_message(
                            self.cfg.bot_chat_id, msg, parse_mode="HTML"
                        )

                    return {
                        "status": "ok",
                        "message": f"{target_name} is already running",
                    }
            except Exception as e:
                logger.warning(f"Pre-flight status check failed: {e}")
                # We proceed if checking fails, hoping the main logic might succeed or catch it later

            logger.info(f"Switching to {target_name} (ID: {target_id})")

            # --- Progress Reporting Setup ---
            progress_msg: Optional[Message] = None
            try:
                progress_msg = await self.bot.send_message(
                    self.cfg.bot_chat_id,
                    f"🔄 <b>Switching to {target_name}...</b>\n⏳ Initializing...",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"Failed to send init message: {e}")

            async def report(text: str):
                """Helper to edit the existing progress message."""
                if not progress_msg:
                    return
                try:
                    await self.bot.edit_message_text(
                        text,
                        chat_id=self.cfg.bot_chat_id,
                        message_id=progress_msg.message_id,
                        parse_mode="HTML",
                    )
                except TelegramBadRequest:
                    # Ignore "message is not modified" errors
                    pass
                except Exception as e:
                    logger.warning(f"Failed to update progress message: {e}")

            # -------------------------------

            # 2. Shutdown Source
            await report(
                f"🔄 <b>Switching to {target_name}...</b>\n🛑 Shutting down {source_name}..."
            )

            if self.get_vm_status(source_id) == "running":
                try:
                    self._get_node().qemu(source_id).status.shutdown.post()
                except ResourceException as e:
                    logger.error(f"Error triggering shutdown: {e}")

                # Wait for clean shutdown
                stopped = await self._wait_for_shutdown(source_id)

                if not stopped:
                    await report(
                        f"🔄 <b>Switching to {target_name}...</b>\n⚠️ {source_name} stuck. Force stopping..."
                    )
                    try:
                        self._get_node().qemu(source_id).status.stop.post()
                        await asyncio.sleep(3)  # Give Proxmox a moment to kill it
                    except Exception as e:
                        msg = f"Critical error stopping {source_name}: {e}"
                        logger.error(msg)
                        await report(f"❌ <b>Switch Failed</b>\n{msg}")
                        return {"status": "error", "message": msg}

            # 3. Start Target
            await report(
                f"🔄 <b>Switching to {target_name}...</b>\n🚀 Starting {target_name}..."
            )
            try:
                current_target_status = self.get_vm_status(target_id)
                if current_target_status != "running":
                    self._get_node().qemu(target_id).status.start.post()
                    await report(
                        f"✅ <b>Switched to {target_name}</b>\n{target_name} is starting."
                    )
                else:
                    await report(f"ℹ️ <b>Info</b>\n{target_name} is already running.")
            except Exception as e:
                msg = f"Failed to start {target_name}: {e}"
                logger.error(msg)
                await report(f"❌ <b>Switch Failed</b>\n{msg}")
                return {"status": "error", "message": msg}

            return {"status": "ok", "message": f"Switched to {target_name}"}


# -----------------------------------------------------------------------------
# 3. Telegram Bot Handlers & Router
# -----------------------------------------------------------------------------

router = Router()


class IsAdminChat(Filter):
    """Filter to only allow commands from the specific admin chat."""

    async def __call__(self, message: Message, config: Config) -> bool:
        return message.chat.id == config.bot_chat_id


@router.message(Command("status"), IsAdminChat())
async def cmd_status(message: Message, vm_controller: VMController):
    stats = vm_controller.get_full_status()
    txt = (
        f"🖥 <b>System Status</b>\n"
        f"🐧 Linux: <code>{stats['linux']}</code>\n"
        f"🪟 Windows: <code>{stats['windows']}</code>\n"
        f"🔒 Locked: <code>{stats['locked']}</code>"
    )
    await message.answer(txt, parse_mode="HTML")


@router.message(Command("lock"), IsAdminChat())
async def cmd_lock(message: Message, vm_controller: VMController):
    vm_controller.set_lock(True)
    await message.answer(
        "🔒 System <b>LOCKED</b>. Auto-switching disabled.", parse_mode="HTML"
    )


@router.message(Command("unlock"), IsAdminChat())
async def cmd_unlock(message: Message, vm_controller: VMController):
    vm_controller.set_lock(False)
    await message.answer("🔓 System <b>UNLOCKED</b>.", parse_mode="HTML")


@router.message(Command("help"), IsAdminChat())
async def cmd_help(message: Message):
    txt = (
        "🤖 <b>Proxmox VM Manager Bot</b>\n\n"
        "<b>Commands:</b>\n"
        "/status - Show current status of VMs and Lock\n"
        "/switch_linux - Switch to Linux VM\n"
        "/switch_windows - Switch to Windows VM\n"
        "/switch - Toggle active VM based on current state\n"
        "/lock - Prevent API/Bot from switching VMs\n"
        "/unlock - Allow switching VMs\n"
        "/help - Show this help message"
    )
    await message.answer(txt, parse_mode="HTML")


@router.message(Command("switch_linux"), IsAdminChat())
async def cmd_linux(message: Message, vm_controller: VMController):
    # perform_switch handles its own Telegram updates (progress/error)
    await vm_controller.perform_switch("linux")


@router.message(Command("switch_windows"), IsAdminChat())
async def cmd_windows(message: Message, vm_controller: VMController):
    # perform_switch handles its own Telegram updates (progress/error)
    await vm_controller.perform_switch("windows")


@router.message(Command("switch"), IsAdminChat())
async def cmd_switch(message: Message, vm_controller: VMController):
    """Intelligently toggles based on current state."""
    status = vm_controller.get_full_status()
    if status["linux"] == "running":
        await cmd_windows(message, vm_controller)
    elif status["windows"] == "running":
        await cmd_linux(message, vm_controller)
    else:
        await message.answer(
            "⚠️ Both VMs are stopped. Use specific command to start one."
        )


async def setup_bot_dispatcher(
    config: Config, vm_controller: VMController
) -> Dispatcher:
    dp = Dispatcher()

    # Register the global router
    dp.include_router(router)

    # Inject dependencies into workflow data so they are available in handlers/filters
    dp["config"] = config
    dp["vm_controller"] = vm_controller

    return dp


# -----------------------------------------------------------------------------
# 4. API Controller (Litestar)
# -----------------------------------------------------------------------------


class VMAPIController(Controller):
    path = "/"

    @get("/")
    async def index(self) -> dict[str, str]:
        return {"app": "Proxmox VM Switcher", "version": "2.0"}

    @get("/status")
    async def get_status(self, vm_controller: VMController) -> dict[str, str]:
        return vm_controller.get_full_status()

    @post("/switch_windows")
    async def switch_windows(self, vm_controller: VMController) -> dict[str, Any]:
        result = await vm_controller.perform_switch("windows", quiet_on_skip=True)
        if result["status"] == "error":
            return result
        return result

    @post("/switch_linux")
    async def switch_linux(self, vm_controller: VMController) -> dict[str, Any]:
        result = await vm_controller.perform_switch("linux", quiet_on_skip=True)
        if result["status"] == "error":
            return result
        return result

    @post("/lock")
    async def lock_system(self, vm_controller: VMController) -> dict[str, bool]:
        vm_controller.set_lock(True)
        return {"locked": True}

    @post("/unlock")
    async def unlock_system(self, vm_controller: VMController) -> dict[str, bool]:
        vm_controller.set_lock(False)
        return {"locked": False}


# -----------------------------------------------------------------------------
# 5. App Lifecycle & Dependency Injection
# -----------------------------------------------------------------------------


async def on_startup(app: Litestar):
    """Initializes the Bot and starts polling in the background."""
    config = app.state.config
    bot = app.state.bot
    vm_controller = app.state.vm_controller

    # Setup Aiogram Dispatcher
    dp = await setup_bot_dispatcher(config, vm_controller)

    # Run bot polling as a background task
    # We store the task in app.state to cancel it later if needed (though Litestar handles loop)
    asyncio.create_task(dp.start_polling(bot))
    logger.info("Telegram Bot polling started")


def create_app() -> Litestar:
    # Initialize dependencies
    config = Config.from_env()
    bot = Bot(token=config.bot_token)
    vm_controller = VMController(config, bot)

    return Litestar(
        route_handlers=[VMAPIController],
        on_startup=[on_startup],
        state=SimpleNamespace(config=config, bot=bot, vm_controller=vm_controller),
        dependencies={
            "vm_controller": Provide(lambda: vm_controller, sync_to_thread=False)
        },
    )


def main() -> None:
    import uvicorn

    uvicorn.run("pve_switch:create_app", factory=True, host="0.0.0.0", port=8000)


# To run: uvicorn vm_manager:create_app --reload
if __name__ == "__main__":
    main()
