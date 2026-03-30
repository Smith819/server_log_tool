# 安装说明 / Installation Guide

## 1. 系统要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Ubuntu 18.04+, Debian 9+, CentOS 7+, RHEL 7+, Fedora 28+ |
| Python | 3.6+（stdlib only，无需 pip 安装额外包）|
| 权限 | root |
| 端口 | 39282（POST服务）, 39283（Multipart服务）|

## 2. 一键安装

```bash
# 克隆或下载项目
git clone <repo-url>
cd autoglm-server

# 执行安装
sudo bash install.sh
```

安装脚本会自动：
- 检测发行版（Ubuntu/Debian/CentOS/RHEL/Fedora）
- 安装 python3（如未安装）
- 创建系统用户 `autoglm`
- 部署文件到 `/opt/autoglm-server/`
- 注册并启动 systemd 服务
- 配置防火墙（如检测到 ufw 或 firewalld）

## 3. 手动安装步骤

如果自动安装失败，可手动执行：

```bash
# 3.1 安装 Python3
# Ubuntu/Debian:
sudo apt-get update && sudo apt-get install -y python3

# CentOS/RHEL 7:
sudo yum install -y python3

# CentOS/RHEL 8+ / Fedora:
sudo dnf install -y python3

# 3.2 创建用户
sudo useradd -r -s /bin/false autoglm

# 3.3 创建目录
sudo mkdir -p /opt/autoglm-server/uploads
sudo mkdir -p /opt/autoglm-server/logs

# 3.4 复制文件
sudo cp server_post.py server_multipart.py config.ini /opt/autoglm-server/
sudo chown -R autoglm:autoglm /opt/autoglm-server/
sudo chmod +x /opt/autoglm-server/server_post.py
sudo chmod +x /opt/autoglm-server/server_multipart.py

# 3.5 安装 systemd 服务
sudo cp services/autoglm-post.service /etc/systemd/system/
sudo cp services/autoglm-multipart.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable autoglm-post autoglm-multipart
sudo systemctl start autoglm-post autoglm-multipart
```

## 4. 验证安装

```bash
# 查看服务状态
sudo systemctl status autoglm-post
sudo systemctl status autoglm-multipart

# 测试端口监听
ss -tlnp | grep -E '39282|39283'

# 快速测试（本机）
curl -X POST http://127.0.0.1:39282/ \
  -d 'file_url=https://www.w3schools.com/css/img_5terre.jpg'
```

## 5. 配置 AutoGLM 应用

安装完成后，在 Android 端 AutoGLM 应用中：

1. 打开设置界面
2. 找到 **SERVER_LOG** 配置项，填入：
   ```
   http://<服务器IP>:39282/
   ```
   （或 Method 2 使用 39283）
3. 找到 **LOG_FILE_URL** 配置项，填入：
   ```
   http://<服务器IP>:39282/test.log
   ```
   (可替换为具体图片名对应的 .log 文件)

## 6. 运行测试脚本

```bash
# 替换 <server-ip> 为实际IP
bash test/test_post.sh <server-ip> 39282
bash test/test_multipart.sh <server-ip> 39283
```

## 7. 查看日志

```bash
# 服务器处理日志
tail -100f /opt/autoglm-server/logs/server_post.log
tail -100f /opt/autoglm-server/logs/server_multipart.log

# 查看已下载的图片
ls -la /opt/autoglm-server/uploads/

# 查看单个文件的日志
cat /opt/autoglm-server/uploads/111.log
cat /opt/autoglm-server/uploads/111_download.log
```

## 8. 常见问题

### Q: 服务启动失败
```bash
journalctl -u autoglm-post -n 50 --no-pager
```

### Q: 端口被占用
```bash
sudo lsof -i :39282
# 修改 config.ini 中的端口，然后重启服务
```

### Q: 图片下载失败
- 检查服务器网络能否访问图片 URL
- 查看 `uploads/<name>_download.log` 中的错误信息
- 检查 `logs/server_post.log`

### Q: Android 端上传失败
- 确认服务器 IP 和端口正确
- 确认防火墙已开放端口
- 先用 curl 在本地测试

### Q: CentOS 7 python3 版本过旧
```bash
sudo yum install -y centos-release-scl
sudo yum install -y rh-python36
scl enable rh-python36 bash
```

## 9. 升级

```bash
sudo systemctl stop autoglm-post autoglm-multipart
sudo cp server_post.py server_multipart.py /opt/autoglm-server/
sudo systemctl start autoglm-post autoglm-multipart
```

## 10. 卸载

```bash
sudo bash uninstall.sh
```

## 11. Import Existing .cer/.key Certificates

If you already have a certificate and private key:

```bash
sudo bash /opt/autoglm-server/import_tls_cert.sh /path/to/cert-or-bundle-dir

# If cert and key are stored separately:
sudo bash /opt/autoglm-server/import_tls_cert.sh /path/to/cert-dir /path/to/key-dir

# If the key is encrypted:
sudo TLS_KEY_PASSPHRASE='your-passphrase' bash /opt/autoglm-server/import_tls_cert.sh /path/to/cert-dir /path/to/key-dir
```

The import script searches recursively, converts supported certificate formats
to PEM, decrypts encrypted private keys when a passphrase is available, verifies
that Python `ssl` can load the pair, updates `/opt/autoglm-server/config.ini`,
and applies the required `root:autoglm` permissions.
