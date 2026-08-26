# SSL 证书文件

此目录包含用于 WebSocket 服务器的 SSL/TLS 证书文件。

## 文件说明

| 文件 | 说明 |
|------|------|
| `cert.pem` | 自签名 SSL 证书 |
| `key.pem` | SSL 私钥 |

## 当前证书信息

- **类型**: 自签名证书（仅用于开发测试）
- **有效期**: 365天
- **密钥长度**: RSA 2048位
- **CN (Common Name)**: localhost

## 重新生成证书

如果证书过期或需要重新生成，运行以下命令：

```bash
cd ssl
openssl req -x509 -newkey rsa:2048 \
    -keyout key.pem \
    -out cert.pem \
    -days 365 \
    -nodes \
    -subj "/C=CN/ST=Beijing/L=Beijing/O=RobotConnect/OU=Dev/CN=localhost"
```

## 配置使用

在 `robot_config.json` 中配置：

**Docker 环境**（使用绝对路径）：
```json
"websocket_server": {
    "ssl_enabled": true,
    "ssl_cert_file": "/config/ssl/cert.pem",
    "ssl_key_file": "/config/ssl/key.pem"
}
```

**本地开发环境**（使用相对路径）：
```json
"websocket_server": {
    "ssl_enabled": true,
    "ssl_cert_file": "config/ssl/cert.pem",
    "ssl_key_file": "config/ssl/key.pem"
}
```

## Docker 外部挂载

运行 Docker 时，将整个 config 目录挂载到容器的 /config：

```bash
docker run -d --name robot_connect \
    -p 8090:8090 \
    -p 8091:8091 \
    -v /path/to/your/config:/config \
    robot_connect:arm-autostart
```

外部 config 目录结构：
```
/path/to/your/config/
├── robot_config.json
└── ssl/
    ├── cert.pem
    └── key.pem
```

## 客户端连接

由于是自签名证书，客户端需要禁用证书验证：

```python
import ssl
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE
```

## 生产环境注意事项

**自签名证书仅用于开发测试！**

生产环境应使用：
1. 由受信任 CA 签发的证书
2. Let's Encrypt 免费证书
3. 企业内部 CA 签发的证书
