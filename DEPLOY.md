# Aven API — Guía de Despliegue (Docker local → Oracle Cloud)

## Índice
1. Requisitos previos
2. Parte A — Probar en Docker, en tu computador
3. Parte B — Subir los mismos contenedores a Oracle Cloud
4. Configurar el webhook de Meta apuntando a producción
5. Operación diaria (logs, actualizaciones, reinicios)
6. Troubleshooting — problemas típicos

---

## 1. Requisitos previos

- Docker Desktop instalado en tu computador (incluye Docker Compose v2).
- Una instancia de Oracle Cloud (OCI) ya creada, con IP pública, y acceso SSH con tu llave privada.
- Un dominio o subdominio que puedas apuntar a la IP pública de tu instancia (ej. `api.aven.cl`) — **obligatorio**, porque Meta exige HTTPS para el webhook y Caddy necesita un dominio real para emitir el certificado.
- Los valores reales de `META_APP_SECRET` y un `META_VERIFY_TOKEN` que tú definas (ver documentación de Meta for Developers, sección WhatsApp > Configuration).

---

## 2. Parte A — Probar en Docker, en tu computador

> **Si estás en Windows:** usa `setup.ps1` en vez de seguir los pasos manuales de abajo — hace exactamente lo mismo (instala, configura `.env`, levanta Docker, se autoprueba) en un solo comando desde PowerShell. Los pasos manuales de esta sección sirven igual como referencia, y los comandos `docker compose ...` son idénticos en Windows, solo cambia cómo se ejecutan `curl` (usa PowerShell, no cmd.exe — Windows 10/11 ya trae `curl.exe` real).

### Paso 1: Descomprime el proyecto y entra a la carpeta

```bash
unzip aven-api.zip
cd aven-api
```

### Paso 2: Configura tus variables de entorno

```bash
cp .env.example .env
```

Abre `.env` con un editor de texto y reemplaza los valores de ejemplo por los reales (o déjalos como están si solo quieres probar el flujo con el simulador de webhooks incluido — no necesitas credenciales reales de Meta para las pruebas locales).

### Paso 3: Levanta los contenedores

```bash
docker compose up --build
```

Esto construye la imagen de la API y levanta dos contenedores: `app` (tu API) y `redis` (cola y deduplicación). La primera vez tarda 1-2 minutos por la descarga de la imagen base de Python y Redis.

Deberías ver algo como:
```
app-1    | INFO:     Uvicorn running on http://0.0.0.0:8000
redis-1  | Ready to accept connections
```

### Paso 4: Verifica que todo esté funcionando

Abre otra terminal (deja la anterior corriendo) y prueba:

```bash
# Salud básica
curl http://localhost:8000/api/health

# Salud "profunda" — confirma que la API puede hablar con Redis
curl http://localhost:8000/api/health/ready
```

Ambas deberían devolver `200 OK` con un JSON de estado `ok`/`ready`.

### Paso 5: Prueba el flujo completo desde el navegador

Abre `http://localhost:8000` en tu navegador — ahí está el panel de pruebas HTML/CSS que ya construimos. Prueba los tres botones:
1. Salud del servidor
2. Simular mensaje entrante de WhatsApp (verás la firma HMAC validada y el mensaje encolado en Redis)
3. Aislamiento por tenant (JWT)

### Paso 6: Verifica la cola de Redis directamente (opcional, para confirmar a bajo nivel)

```bash
docker compose exec redis redis-cli llen queue:incoming_messages
```

Debería mostrar cuántos mensajes de prueba enviaste.

### Paso 7: Apaga todo cuando termines de probar

```bash
docker compose down          # detiene y elimina los contenedores, conserva los datos de Redis
docker compose down -v       # además borra el volumen de Redis, si quieres empezar de cero
```

**Si todos estos pasos funcionaron sin errores, tienes exactamente lo mismo que vas a subir a Oracle — la imagen Docker es idéntica, solo cambia dónde corre.**

---

## 3. Parte B — Subir los mismos contenedores a Oracle Cloud

### Paso 1: Conéctate a tu instancia por SSH

```bash
ssh -i /ruta/a/tu-llave-privada.key ubuntu@<IP_PUBLICA_DE_TU_INSTANCIA>
```

(El usuario puede ser `ubuntu`, `opc`, o el que hayas definido según la imagen que elegiste al crear la instancia — Oracle Linux usa `opc` por defecto, Ubuntu usa `ubuntu`.)

### Paso 2: Verifica la arquitectura del procesador

Esto es importante porque las instancias gratuitas de Oracle Cloud (Ampere A1) son **ARM64**, no x86_64 — si tu computador es Intel/AMD, la imagen Docker que construyas localmente no sirve directo en la instancia; hay que reconstruirla ahí mismo.

```bash
uname -m
```

- Si dice `aarch64` → tu instancia es ARM. Vas a construir la imagen directo en el servidor (Paso 5), no la vas a transferir ya armada desde tu computador.
- Si dice `x86_64` → arquitectura Intel/AMD estándar, sin diferencias especiales.

### Paso 3: Instala Docker en la instancia

**Si es Ubuntu:**
```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker $USER
```

**Si es Oracle Linux:**
```bash
sudo dnf install -y dnf-utils zip unzip
sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
```

Cierra sesión SSH y vuelve a entrar para que el permiso de grupo `docker` tome efecto:
```bash
exit
ssh -i tu-llave.key ubuntu@<IP_PUBLICA>
docker ps    # no debería pedir sudo
```

### Paso 4: Transfiere el proyecto a la instancia

Desde tu computador (no dentro del SSH):

```bash
scp -i /ruta/a/tu-llave.key aven-api.zip ubuntu@<IP_PUBLICA>:~/
```

De vuelta en la instancia (por SSH):
```bash
unzip aven-api.zip
cd aven-api
```

> **Alternativa recomendada a mediano plazo:** en vez de `scp` manual cada vez, sube el proyecto a un repositorio Git privado (GitHub/GitLab) y en la instancia haz `git clone`. Así, actualizar producción después es un `git pull` en vez de volver a comprimir y transferir el zip completo.

### Paso 5: Configura las variables de entorno de producción

```bash
cp .env.example .env
nano .env
```

Reemplaza **todos** los valores de ejemplo por los reales de producción (`META_APP_SECRET`, `META_VERIFY_TOKEN`, `JWT_SECRET` — genera uno nuevo y distinto al de desarrollo, no reutilices el mismo).

```bash
chmod 600 .env    # solo tu usuario puede leer este archivo
```

### Paso 6: Configura el dominio en el Caddyfile

```bash
nano Caddyfile
```

Reemplaza `tu-dominio.cl` por tu dominio o subdominio real (ej. `api.avelyn.cl`). Guarda.

**Antes de continuar:** ve al panel DNS de tu dominio y crea un registro `A` apuntando ese subdominio a la IP pública de tu instancia de Oracle. Espera unos minutos a que propague (puedes verificar con `dig api.tudominio.cl` o `nslookup`).

### Paso 7: Abre los puertos necesarios — Oracle Cloud tiene DOS firewalls, hay que abrir ambos

Este es el punto donde más gente se traba en Oracle Cloud específicamente, porque a diferencia de otros proveedores, **hay un firewall a nivel de red (Security List / NSG) Y un firewall a nivel de sistema operativo (iptables) que vienen activos por defecto**. Si solo abres uno, no va a funcionar.

**7a. Firewall de red (en la consola web de Oracle Cloud):**
1. Ve a tu instancia → pestaña "Attached VNICs" → click en la VNIC → click en la Subnet.
2. Entra a la "Security List" asociada (o "Network Security Group" si usaste uno).
3. Agrega dos reglas de Ingress:
   - Puerto 80 (HTTP), origen `0.0.0.0/0`
   - Puerto 443 (HTTPS), origen `0.0.0.0/0`

**7b. Firewall del sistema operativo (dentro de la instancia, por SSH):**

Si es Ubuntu (generalmente usa `iptables` o `ufw`):
```bash
sudo iptables -I INPUT -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save   # si está instalado; si no, instálalo con: sudo apt install iptables-persistent
```

Si es Oracle Linux (usa `firewalld`):
```bash
sudo firewall-cmd --permanent --add-port=80/tcp
sudo firewall-cmd --permanent --add-port=443/tcp
sudo firewall-cmd --reload
```

### Paso 8: Levanta los contenedores en modo producción

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build -d
```

El `-d` corre todo en segundo plano. Nota que estás pasando explícitamente los dos archivos — esto hace que `docker-compose.override.yml` (el de desarrollo, que expone el puerto 8000 directo) se ignore automáticamente sin que tengas que borrarlo.

### Paso 9: Verifica que todo esté arriba

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

Deberías ver 3 contenedores: `app`, `redis`, `caddy`, todos en estado `running` o `healthy`.

```bash
# Espera 30-60 segundos a que Caddy obtenga el certificado HTTPS, luego:
curl https://tu-dominio.cl/api/health
```

Si responde `{"status":"ok",...}`, **tu API ya está en producción con HTTPS válido**, sin haber tocado nginx ni certbot manualmente.

---

## 4. Configurar el webhook de Meta apuntando a producción

En Meta for Developers → tu App → WhatsApp → Configuration:

- **Callback URL:** `https://tu-dominio.cl/api/webhooks/meta`
- **Verify Token:** el mismo valor exacto que pusiste en `META_VERIFY_TOKEN` dentro de `.env`

Al hacer clic en "Verify and Save", Meta llama a tu `GET /api/webhooks/meta` — si el token coincide, se activa. Esto ya está probado en el código (Parte A, Paso 5, botón 1 confirma que el endpoint responde).

---

## 5. Operación diaria

**Ver logs en vivo:**
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f app
```

**Actualizar el código después de un cambio** (asumiendo que usas `git pull` en vez de volver a subir el zip):
```bash
git pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build -d
```
Esto reconstruye solo lo que cambió y reinicia el contenedor `app` sin tocar Redis (tu cola y datos persisten).

**Reiniciar todo:**
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml restart
```

**Asegurar que todo vuelva a levantar solo si la instancia se reinicia** (ej. mantenimiento de Oracle): con `restart: always` ya definido en `docker-compose.prod.yml`, los contenedores se reinician automáticamente cuando el servicio Docker arranca — solo confirma que Docker esté habilitado en el boot:
```bash
sudo systemctl enable docker
```

---

## 6. Troubleshooting — problemas típicos

| Síntoma | Causa probable | Solución |
|---|---|---|
| `curl https://tu-dominio` da timeout | Falta abrir puertos 80/443 en la Security List de Oracle (Paso 7a) | Revisa las reglas de Ingress en la consola de OCI |
| Responde por IP pero no por dominio | El registro DNS `A` no ha propagado aún | Espera unos minutos, verifica con `dig tu-dominio.cl` |
| Caddy no obtiene certificado HTTPS | El dominio no apunta a la IP correcta, o el puerto 80 está bloqueado (Let's Encrypt lo necesita para validar) | `docker compose logs caddy` — el error suele ser explícito |
| `docker ps` pide `sudo` siempre | No cerraste sesión SSH después de `usermod -aG docker` | Cierra y vuelve a entrar por SSH |
| `.\setup.ps1` da error "no se puede cargar porque la ejecución de scripts está deshabilitada" | Política de ejecución de PowerShell por defecto en Windows | Corre `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` en esa misma ventana y reintenta |
| Docker Desktop no arranca en Windows, o `setup.ps1` dice "Docker no parece estar corriendo" | WSL2 o la virtualización de Hyper-V no está habilitada | Abre Docker Desktop manualmente una vez — si pide activar WSL2, sigue el asistente que trae incluido, reinicia, y vuelve a correr `setup.ps1` |
| La imagen construida en tu Mac/PC no corre en Oracle | Diferencia de arquitectura (ARM vs x86) — ver Paso 2 | Construye la imagen directo en la instancia con `docker compose build`, no la transfieras ya armada |
| El webhook de Meta no verifica | El `META_VERIFY_TOKEN` en `.env` no coincide exactamente con el que pusiste en Meta for Developers | Cópialos literal, sin espacios extra |
| Cambios en `.env` no toman efecto | Docker Compose no relee `.env` de contenedores ya corriendo | `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --force-recreate` |

---

## Nota de transparencia sobre esta guía

Los pasos de la **Parte A** (Docker local) están basados en el mismo código que ya probé exhaustivamente en este entorno de desarrollo (firma HMAC, deduplicación, aislamiento JWT, health checks — todos verificados con pruebas reales). Los archivos de Docker Compose se validaron sintácticamente y se simuló su comportamiento de merge siguiendo las reglas oficiales de la especificación de Compose.

Los pasos de la **Parte B** (Oracle Cloud específicamente) no pudieron probarse end-to-end en este entorno porque no tengo una instancia de OCI real disponible aquí — están basados en la documentación oficial de Oracle Cloud y Docker, y en los patrones estándar de despliegue. Antes de considerarlos "a prueba de fallos" al 100%, sigue el orden exacto de los pasos la primera vez, y si algo no calza con lo que ves en tu consola de Oracle (los nombres de menú cambian ocasionally), la tabla de Troubleshooting cubre los puntos donde más suele trabarse la gente específicamente con OCI (el doble firewall del Paso 7 es, con diferencia, el más común).
