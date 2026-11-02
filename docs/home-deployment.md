# 家用 common VM 部署

目标为 `common` VM 102，通过现有 Cloudflare Tunnel → Nginx → TOTP 网关访问。
面板入口为 `https://longhu.970907.xyz`，导航页增加“龙湖签到”。

`compose.home.yaml` 是独立部署文件，使用时必须显式指定 `-f`，不要与默认
Compose 文件合并。它将端口绑定到 `127.0.0.1:8088`，只允许服务器本机访问；
面板和 API 的身份认证由现有 TOTP 网关完成，应用本身没有独立账号系统。

## 初次启动

从自己的 GitHub 仓库克隆部署分支，数据库、抓包、虚拟环境和私钥不进 Git。
`main` 保留上游代码，家用部署和后续改动在 `codex/home-deployment` 分支继续。
准备数据目录并启动：

```sh
git clone --branch codex/home-deployment https://github.com/woshichenbenshuai/longhu-checkin.git /opt/stacks/longhu-checkin
cd /opt/stacks/longhu-checkin
install -d -m 0700 -o 10001 -g 10001 data
printf 'IMAGE_TAG=home-%s\n' "$(git rev-parse --short HEAD)" > .env
chmod 600 .env
docker compose -f compose.home.yaml config --quiet
docker compose -f compose.home.yaml up -d --build
docker compose -f compose.home.yaml ps
```

容器以 UID/GID `10001:10001` 运行，根文件系统只读，内存上限 256MB、CPU
上限 0.5 核；数据库仅写入 `data`，临时文件位于受限 tmpfs，日志自动轮转。
数据库仍包含明文凭据，数据目录必须限制权限，不应纳入普通文件共享。

## 现有网关接入

Nginx 新增独立虚拟主机，沿用认证片段：

```nginx
server {
    listen 127.0.0.1:18080;
    server_name longhu.970907.xyz;
    include snippets/homelab-auth-proxy.conf;
    client_max_body_size 2m;
    location / {
        proxy_read_timeout 180s;
        proxy_pass http://127.0.0.1:8088;
    }
}
```

Cloudflare Tunnel 新增该 hostname，service 为 `http://127.0.0.1:18080`；
不要将隧道直接指向 `8088`，否则会绕过 TOTP。导航链接使用 HTTPS 域名。
现有认证 Cookie 域是 `.970907.xyz`，可以沿用已有登录会话。

## 验收

- 容器 healthy，只有 `127.0.0.1:8088` 端口映射。
- 未登录访问域名首页或 `/api/settings` 均重定向到 TOTP 登录页。
- 登录后可访问面板，API 返回账号数量和下次执行时间。
- 重启容器后数据库与设置保留，容器仍 healthy。
- 初始不导入账号、推送地址为空，部署检查不会触发真实签到或手机通知。

## 通知与凭据

默认仅允许 HTTPS 的 `api.day.app`、`qyapi.weixin.qq.com` 和
`oapi.dingtalk.com`。自建 Bark 需要运维人员通过 `.env` 中
`WEBHOOK_ALLOWED_HOSTS` 显式增加准确域名，并重建容器。
通知与龙湖请求都禁止重定向，浏览器跨站写请求会被拒绝。

部署后由用户在受 TOTP 保护的页面导入自己的 cURL、HAR 或 token，
无需在聊天中粘贴真实凭据。默认每日 08:00–09:00 随机执行。

## 备份与更新

运行中备份 SQLite 使用 SQLite backup API，避免直接复制正在写入的文件：

```sh
docker exec longhu-checkin python -c "import sqlite3; s=sqlite3.connect('/data/longhu.db'); d=sqlite3.connect('/data/longhu.backup.db'); s.backup(d); d.close(); s.close()"
```

备份同样含敏感凭据，应限制权限。后续在本地提交并推送到部署分支后，服务器更新：

```sh
cd /opt/stacks/longhu-checkin
git status --short
git pull --ff-only origin codex/home-deployment
printf 'IMAGE_TAG=home-%s\n' "$(git rev-parse --short HEAD)" > .env
docker compose -f compose.home.yaml up -d --build
docker compose -f compose.home.yaml ps
```

镜像依赖由 `requirements.lock` 锁定版本与哈希；更新依赖时一并重做漏洞扫描。
回滚使用旧镜像 tag；网关和导航修改前应保留各自配置备份。
