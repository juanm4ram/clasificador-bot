# Crea el proyecto de Google Cloud (si no existe), despliega el bot en Cloud Run y conecta el
# webhook de Telegram. Revisa todo antes de empezar y se detiene en el primer error con un mensaje claro.
#
# Uso (desde la carpeta del proyecto):
#   powershell -ExecutionPolicy Bypass -File .\desplegar.ps1 -Proyecto mi-id-de-proyecto

param(
    [string]$Proyecto = "clasificador-bot",
    [string]$Region = "us-central1",
    [string]$Servicio = "clasificador-bot"
)

function Fallar($mensaje) {
    Write-Host "`nERROR: $mensaje" -ForegroundColor Red
    exit 1
}

function Paso($mensaje) {
    Write-Host "`n==> $mensaje" -ForegroundColor Cyan
}

function Ejecutar {
    & gcloud @args
    if ($LASTEXITCODE -ne 0) { Fallar "falló el comando: gcloud $args" }
}

Set-Location $PSScriptRoot

# --- 1. Revisar env.yaml -------------------------------------------------------
Paso "Revisando env.yaml"
if (-not (Test-Path env.yaml)) { Fallar "no existe env.yaml. Copiá env.example.yaml como env.yaml y completalo." }
$cfg = @{}
foreach ($l in Get-Content env.yaml) {
    if ($l -match '^([A-Z_]+):\s*["'']?(.*?)["'']?\s*$') { $cfg[$Matches[1]] = $Matches[2] }
}
if (-not $cfg.TELEGRAM_TOKEN -or $cfg.TELEGRAM_TOKEN -match 'ABC-tu-token') {
    Fallar "TELEGRAM_TOKEN en env.yaml todavía es el de ejemplo. Poné el token de @BotFather y GUARDÁ el archivo."
}
if ($cfg.WEBHOOK_SECRET -eq 'una_cadena_larga_y_aleatoria' -or $cfg.WEBHOOK_SECRET -notmatch '^[A-Za-z0-9_-]{16,256}$') {
    Fallar "WEBHOOK_SECRET en env.yaml: inventá un texto de al menos 16 caracteres, solo letras, números, _ o - (sin espacios)."
}

# --- 2. Revisar el token con Telegram ------------------------------------------
Paso "Verificando el token con Telegram"
$api = "https://api.telegram.org/bot$($cfg.TELEGRAM_TOKEN)"
try { $bot = (Invoke-RestMethod "$api/getMe").result }
catch { Fallar "Telegram rechazó el token. Copiá de nuevo el token completo de @BotFather en env.yaml." }
Write-Host "Token OK: bot @$($bot.username)" -ForegroundColor Green

# --- 3. Revisar gcloud ---------------------------------------------------------
Paso "Verificando Google Cloud CLI"
if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    Fallar "no se encuentra gcloud. Si recién lo instalaste, cerrá PowerShell, abrí una ventana nueva y volvé a ejecutar este script."
}
$cuenta = & gcloud auth list --filter=status:ACTIVE --format="value(account)" 2>$null
if (-not $cuenta) {
    Paso "Iniciando sesión en Google (se abre el navegador)"
    Ejecutar auth login
}

# --- 4. Proyecto y facturación -------------------------------------------------
Paso "Verificando el proyecto $Proyecto"
& gcloud projects describe $Proyecto --format "value(projectId)" 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    if ($Proyecto -notmatch '^[a-z][a-z0-9-]{4,28}[a-z0-9]$') {
        Fallar "ID de proyecto inválido: 6 a 30 caracteres, minúsculas, números y guiones, empezando con una letra."
    }
    Paso "Creando el proyecto $Proyecto"
    & gcloud projects create $Proyecto --name $Proyecto
    if ($LASTEXITCODE -ne 0) { Fallar "no se pudo crear el proyecto. El ID tiene que ser único en todo Google Cloud: probá con otro (ej. $Proyecto-$(Get-Random -Maximum 9999))." }
}
Ejecutar config set project $Proyecto

$facturacion = & gcloud billing projects describe $Proyecto --format "value(billingEnabled)" 2>$null
if ($facturacion -ne "True") {
    Paso "Vinculando la cuenta de facturación"
    $cuentaFact = & gcloud billing accounts list --filter "open=true" --format "value(name)" --limit 1 2>$null
    if (-not $cuentaFact) {
        Fallar "no tenés una cuenta de facturación activa. Creala en https://console.cloud.google.com/billing y volvé a ejecutar el script."
    }
    Ejecutar billing projects link $Proyecto --billing-account $cuentaFact --format none
}

# --- 5. Desplegar --------------------------------------------------------------
Paso "Habilitando servicios de Google Cloud (puede tardar 1-2 minutos)"
Ejecutar services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com

# En proyectos nuevos, la cuenta que construye la imagen (la cuenta de servicio de Compute por
# defecto) no tiene permisos: le damos el rol "Cloud Run Builder". Si ya lo tiene, no cambia nada.
Paso "Dando permiso de build a la cuenta de servicio del proyecto"
$numero = & gcloud projects describe $Proyecto --format "value(projectNumber)"
if (-not $numero) { Fallar "no se pudo obtener el número del proyecto $Proyecto." }
Ejecutar projects add-iam-policy-binding $Proyecto --member "serviceAccount:$numero-compute@developer.gserviceaccount.com" `
    --role roles/run.builder --condition None --quiet --format none

Paso "Desplegando en Cloud Run (tarda entre 5 y 10 minutos)"
Ejecutar run deploy $Servicio --source . --region $Region --allow-unauthenticated --env-vars-file env.yaml `
    --memory 4Gi --cpu 2 --cpu-boost --concurrency 1 --max-instances 2 --timeout 60 --quiet

$url = & gcloud run services describe $Servicio --region $Region --format "value(status.url)"
if (-not $url) { Fallar "no se pudo obtener la URL del servicio." }

# --- 6. Conectar Telegram ------------------------------------------------------
Paso "Conectando Telegram con $url"
$r = Invoke-RestMethod -Method Post -Uri "$api/setWebhook" -Body @{
    url = "$url/webhook"; secret_token = $cfg.WEBHOOK_SECRET; allowed_updates = '["message"]'
}
if (-not $r.ok) { Fallar "Telegram no aceptó el webhook: $($r.description)" }

$info = (Invoke-RestMethod "$api/getWebhookInfo").result
if ($info.last_error_message) { Write-Host "Aviso de Telegram: $($info.last_error_message)" -ForegroundColor Yellow }

Write-Host "`nLISTO. Escribile a @$($bot.username) en Telegram, por ejemplo: 'me cobraron dos veces la factura'." -ForegroundColor Green
Write-Host "El primer mensaje puede tardar 20-30 segundos (el servidor arranca y carga el modelo)."
Write-Host "Logs: gcloud run services logs read $Servicio --region $Region"
