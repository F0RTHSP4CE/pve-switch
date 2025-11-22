# pve-switch

**pve-switch** is a specialized tool designed to manage and switch between a Linux and a Windows Virtual Machine (VM) on a Proxmox VE server. It ensures that only one of the two VMs is running at any given time, effectively turning your Proxmox server into a dual-boot-like system that can be controlled remotely.

The system is built with Python and provides both a **Telegram Bot** interface and a **REST API** for control.

## Features

-   **Smart Switching**: Automatically shuts down the currently running VM (Linux or Windows) before starting the other.
-   **Telegram Bot Integration**: Control your VMs directly from a Telegram chat.
    -   `/switch_linux`: Switch to Linux.
    -   `/switch_windows`: Switch to Windows.
    -   `/status`: View current status of VMs and system lock.
    -   `/lock` & `/unlock`: Prevent accidental switching.
-   **REST API**: Integrate with other tools using HTTP endpoints.
-   **Safety Locks**: Prevents concurrent operations and allows manual locking of the system.
-   **Auto-Shutdown**: Waits for a clean shutdown of the source VM, with a force-stop fallback if it gets stuck.

## Prerequisites

-   **Proxmox VE**: A running Proxmox VE server.
-   **Python 3.13+**: For running the application locally.
-   **Telegram Bot**: A bot token from @BotFather.
-   **Proxmox API Token**: A user and API token with permissions to manage VMs.

## Configuration

The application is configured via environment variables. You can use a `.env` file (see `.env.example`).

| Variable | Description |
| :--- | :--- |
| `PROXMOX_HOST` | Hostname or IP of your Proxmox server. |
| `PROXMOX_USER` | Proxmox user (e.g., `root@pam`). |
| `PROXMOX_TOKEN_NAME` | Name of the API token. |
| `PROXMOX_TOKEN_VALUE` | Secret value of the API token. |
| `PROXMOX_NODE_NAME` | Name of the Proxmox node hosting the VMs. |
| `PROXMOX_LINUX_VM_ID` | VM ID of the Linux VM. |
| `PROXMOX_WIN_VM_ID` | VM ID of the Windows VM. |
| `BOT_TOKEN` | Telegram Bot Token. |
| `BOT_CHAT_ID` | The Telegram Chat ID authorized to issue commands. |
| `LOCK_FILE_PATH` | Path to the lock file (default: `lock.local`). |

## Installation & Running

### Local Development

This project uses [uv](https://github.com/astral-sh/uv) for dependency management.

1.  **Clone the repository**:
    ```bash
    git clone <repository-url>
    cd pve-switch
    ```

2.  **Install dependencies**:
    ```bash
    uv sync
    ```

3.  **Run the server**:
    You can use the provided `Justfile` (requires `just`) or run directly with `uvicorn`.
    ```bash
    # Using just
    just run

    # Or directly
    uvicorn pve_switch:create_app --factory --reload
    ```

### Docker

A pre-built image is available at `ghcr.io/f0rthsp4ce/pve-switch`.

1.  **Run the container**:
    ```bash
    docker run -d \
      --env-file .env \
      -v $(pwd)/lock:/app/lock.local \
      -p 8000:8000 \
      ghcr.io/f0rthsp4ce/pve-switch
    ```
    *Note: Mounting `lock.local` allows the lock state to persist across restarts.*

    Alternatively, you can build it locally:
    ```bash
    docker build -t pve-switch .
    ```

## Usage

### Telegram Commands

Send these commands to your bot:

-   `/status`: Check which VM is running.
-   `/switch`: Toggle to the other VM.
-   `/switch_linux`: Force switch to Linux.
-   `/switch_windows`: Force switch to Windows.
-   `/lock`: Disable switching.
-   `/unlock`: Enable switching.

### API Endpoints

The application exposes a REST API on port 8000 (default).

-   `GET /status`: Get JSON status of VMs.
-   `POST /switch_linux`: Switch to Linux.
-   `POST /switch_windows`: Switch to Windows.
-   `POST /lock`: Lock the system.
-   `POST /unlock`: Unlock the system.

## License

[Add License Information Here]
