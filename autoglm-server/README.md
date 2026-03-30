# autoglm-server

Linux server for [AutoGLM](https://github.com/) — receives image links from the Android Auto.js app, downloads images, and serves log files.

## 架构说明 / Architecture

AutoGLM (Android) 通过两种方式将图片外链上传到本服务器：

| 方式 | 脚本 | 端口 | 对应 main.js 函数 |
|------|------|------|-------------------|
| Method 1: Form/Text/JSON POST | `server_post.py` | 39282 | `tryUploadLinkDirect()` |
| Method 2: Multipart upload | `server_multipart.py` | 39283 | `tryUploadLinkAsLogFile()` |

服务器收到链接后：
1. 验证是否为合法图片链接（.jpg/.jpeg/.png/.gif/.webp/.bmp）
2. 后台下载图片到 `uploads/` 目录
3. 生成两个日志文件（对应 main.js 中 `showServerLogActionFloaty` 的日志逻辑）：
   - `uploads/111.log` — 收到链接的记录（未经 run_temple 处理）
   - `uploads/111_download.log` — 下载结果记录
4. 通过 HTTP GET 提供文件下载（供 Android 端 `downloadServerLogToLocal()` 拉取）

## 快速开始 / Quick Start

```bash
git clone <this-repo>
cd autoglm-server
sudo bash install.sh
```

安装后在 AutoGLM 应用中配置：
- **SERVER_LOG**: `http://<your-server-ip>:39282/`（或 39283）
- **LOG_FILE_URL**: `http://<your-server-ip>:39282/111.log`

## 文件结构 / File Structure

```
autoglm-server/
├── server_post.py          # Method 1 服务 (port 39282)
├── server_multipart.py     # Method 2 服务 (port 39283)
├── config.ini              # 配置文件（含 [tls] 节）
├── install.sh              # 一键安装脚本
├── uninstall.sh            # 卸载脚本
├── setup_tls.sh            # TLS 证书生成/配置脚本
├── services/
│   ├── autoglm-post.service
│   └── autoglm-multipart.service
├── test/
│   ├── test_post.sh        # Method 1 测试 (HTTP/HTTPS)
│   └── test_multipart.sh   # Method 2 测试 (HTTP/HTTPS)
└── README.md
```

## 配置 / Configuration

编辑 `/opt/autoglm-server/config.ini`：

```ini
[server]
post_port = 39282
multipart_port = 39283
upload_dir = /opt/autoglm-server/uploads
log_dir = /opt/autoglm-server/logs
download_timeout = 30
max_image_size = 52428800

[tls]
# false = HTTP（默认），true = HTTPS
enabled = false
cert_file = /opt/autoglm-server/certs/server.crt
key_file  = /opt/autoglm-server/certs/server.key
min_tls_version = TLSv1.2
```

修改后重启服务：
```bash
sudo systemctl restart autoglm-post
sudo systemctl restart autoglm-multipart
```

## HTTPS / TLS 配置

两种方式二选一：

### 方式一：自签名证书（局域网/内网推荐）

```bash
# 为 IP 地址生成证书
sudo bash setup_tls.sh self-signed 192.168.0.125

# 或为域名生成证书
sudo bash setup_tls.sh self-signed myserver.local
```

脚本自动：生成 RSA 4096 证书 → 写入 config.ini → 重启服务。

> Android 端需要在设备上安装并信任该自签名证书，或在 main.js 中允许不验证证书。

### 方式二：Let's Encrypt（公网域名推荐）

```bash
sudo bash setup_tls.sh letsencrypt yourdomain.com admin@yourdomain.com
```

脚本自动：安装 certbot → 申请证书 → 写入 config.ini → 重启服务 → 安装自动续期 hook。

### 启用 HTTPS 后在 AutoGLM 中配置

```
SERVER_LOG  → https://<ip-or-domain>:39282/
LOG_FILE_URL → https://<ip-or-domain>:39282/111.log
```

### 测试 HTTPS

```bash
# 自签名证书需加 -k 跳过验证
bash test/test_post.sh 192.168.0.125 39282 https
bash test/test_multipart.sh 192.168.0.125 39283 https
```

## 日志 / Logs

```bash
# 服务运行日志
tail -f /opt/autoglm-server/logs/server_post.log
tail -f /opt/autoglm-server/logs/server_multipart.log

# systemd journal
journalctl -u autoglm-post -f
journalctl -u autoglm-multipart -f
```

## 测试 / Testing

```bash
# 测试 Method 1
bash test/test_post.sh <server-ip> 39282

# 测试 Method 2
bash test/test_multipart.sh <server-ip> 39283

# 本地测试
bash test/test_post.sh 127.0.0.1 39282
bash test/test_multipart.sh 127.0.0.1 39283
```

## 与 main.js 对照 / Correspondence to main.js

| main.js | 服务器行为 |
|---------|------------|
| `PICTURE_CONFIG.SERVER_LOG` | 设置为 `http://<ip>:39282/` 或 `http://<ip>:39283/` |
| `PICTURE_CONFIG.LOG_FILE_URL` | 设置为 `http://<ip>:39282/<name>.log` |
| `tryUploadLinkDirect()` | server_post.py 接收 |
| `tryUploadLinkAsLogFile()` | server_multipart.py 接收 |
| `downloadServerLogToLocal()` | GET `/<name>.log` |
| `showServerLogActionFloaty()` | 显示 `<name>.log` 内容 |
| `buildDownloadLogUrlFromImageLink()` | 生成 `<name>_download.log` URL |

## 系统要求 / Requirements

- Linux (Ubuntu 18.04+, Debian 9+, CentOS 7+, RHEL 7+, Fedora 28+)
- Python 3.6+（通常已预装）
- 开放端口 39282、39283（或自定义端口）
- Root 权限（安装时）

## 防火墙 / Firewall

```bash
# UFW (Ubuntu/Debian)
sudo ufw allow 39282/tcp
sudo ufw allow 39283/tcp

# firewalld (CentOS/RHEL)
sudo firewall-cmd --permanent --add-port=39282/tcp
sudo firewall-cmd --permanent --add-port=39283/tcp
sudo firewall-cmd --reload
```

## 卸载 / Uninstall

```bash
sudo bash uninstall.sh
```
