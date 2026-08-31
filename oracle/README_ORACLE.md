# Деплой pm-recorder на Oracle Cloud (eu-amsterdam-1)

Два шляхи: **A) автоматом через OCI CLI** (рекомендую) або **B) руками в консолі**.
Результат однаковий: Ubuntu VM з Docker'ом, контейнер pm-recorder пише дані у `/opt/recorder/data`.

---

## Крок 0 (один раз): доступ до OCI API

### 0.1 Встанови OCI CLI локально
```bash
uv tool install oci-cli          # або: pip3 install --user oci-cli
oci --version
```

### 0.2 Створи API-ключ у консолі (~2 хв)
1. Згенеруй пару ключів локально:
   ```bash
   mkdir -p ~/.oci && openssl genrsa -out ~/.oci/oci_api_key.pem 2048
   openssl rsa -pubout -in ~/.oci/oci_api_key.pem -out ~/.oci/oci_api_key_public.pem
   cat ~/.oci/oci_api_key_public.pem
   ```
2. У консолі cloud.oracle.com (регіон Amsterdam):
   - іконка профілю (верхній правий) → **My profile / User settings**
   - **API keys → Add API key → Paste public key** → встав вміст публічного ключа → Add
   - скопіюй **Fingerprint**
3. Там же скопіюй: **User OCID** (`ocid1.user.oc1.....`) та **Tenancy OCID** (`ocid1.tenancy.oc1.....`)
   (Tenancy: профіль → Tenancy: <твоя> → copy OCID)

71:a0:35:fb:65:18:16:c2:66:8c:48:a6:48:73:ed:f0
[DEFAULT]
user=ocid1.user.oc1..aaaaaaaazenehf6y2uk2bqlcuq2tvxtz6v4h2yqdz7huylljdwj2jpveyicq
fingerprint=71:a0:35:fb:65:18:16:c2:66:8c:48:a6:48:73:ed:f0
tenancy=ocid1.tenancy.oc1..aaaaaaaaqbffaq6ss2ha2x5ai55eqp26ukmmazb2xayix5izskmqibjgyauq
region=eu-amsterdam-1
key_file=<path to your private keyfile> # TODO

### 0.3 Запиши конфіг
```bash
mkdir -p ~/.oci
cat > ~/.oci/config <<'EOF'
[DEFAULT]
user=ocid1.user.oc1..aaaaaaaazenehf6y2uk2bqlcuq2tvxtz6v4h2yqdz7huylljdwj2jpveyicq
fingerprint=71:a0:35:fb:65:18:16:c2:66:8c:48:a6:48:73:ed:f0
tenancy=ocid1.tenancy.oc1..aaaaaaaaqbffaq6ss2ha2x5ai55eqp26ukmmazb2xayix5izskmqibjgyauq
region=eu-amsterdam-1
key_file=~/.oci/oci_api_key.pem
EOF
chmod 600 ~/.oci/config ~/.oci/oci_api_key.pem
oci iam availability-domain list --query 'data[].name'   # перевірка: має повернути ADs
```

`COMPARTMENT_ID` = Tenancy OCID (якщо не створював окремих компартментів).

---

## Шлях A — автоматичний деплой (одна команда)

```bash
COMPARTMENT_ID=ocid1.tenancy.oc1..xxxx ./oracle/deploy.sh
```

Скрипт сам: знайде Ubuntu 24.04 image, створить VCN/субнет/інтернет-шлюз якщо треба,
відкриє SSH, запустить інстанс **VM.Standard.A1.Flex (2 OCPU / 12 GB)** з cloud-init
(docker + swap ставляться автоматично), дочекается RUNNING і надрукує публічний IP.

Якщо A1 "Out of host capacity" (часта історія на Always Free) → два варіанти:
```bash
SHAPE=VM.Standard.E2.1.Micro COMPARTMENT_ID=... ./oracle/deploy.sh    # x86 micro, завжди вільний
# або спробуй пізніше / інший регіон
```

## Шлях B — руками в консолі (якщо CLI не хочеться)

1. **Compute → Instances → Create instance**
   - Name: `pm-recorder`
   - Image: **Canonical Ubuntu 24.04**
   - Shape: `VM.Standard.A1.Flex` (2 OCPU, 12 GB) або `VM.Standard.E2.1.Micro`
   - SSH keys: **Paste** → вміст `~/.ssh/id_ed25519.pub`
   - Networking: стандартний VCN, **Assign a public IPv4 address** ✓
2. Create → чекати RUNNING → скопіювати Public IP.
3. Docker поставить cloud-init автоматично (якщо створювала інстанс БЕЗ нашого
   cloud-init — виконай вручну:
   ```bash
   ssh ubuntu@IP
   curl -fsSL https://get.docker.com | sudo sh
   sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && \
     sudo mkswap /swapfile && sudo swapon /swapfile && \
     echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
   ```

---

## Фінал (обидва шляхи): залити рекодер і стартувати

```bash
IP=<публічний IP>
rsync -av --exclude data --exclude docker-test-data /home/alex/Project/up_down/recorder/ ubuntu@$IP:/opt/recorder/
ssh ubuntu@$IP
cd /opt/recorder
sudo docker compose up -d --build     # network_mode: host вже прописано
docker logs -f pm-recorder            # має бути: RTDS subscribed / Binance WS connected / CLOB WS subscribed
ls -la data/                          # jsonl файл поточного дня росте
sudo chown -R ubuntu:ubuntu data      # щоб читати без sudo
```

## Зняття даних назад локально
```bash
rsync -av ubuntu@$IP:/opt/recorder/data/ /home/alex/Project/up_down/oracle_data/
```

## Обслуговування
| Дія | Команда |
|---|---|
| статус | `ssh ubuntu@$IP sudo docker ps` |
| логи | `ssh ubuntu@$IP sudo docker logs --tail 100 pm-recorder` |
| рестарт | `ssh ubuntu@$IP 'cd /opt/recorder && sudo docker compose restart'` |
| оновити LUT fair value | перезаписати `recorder/lut/lut_fair_value.csv` → `docker compose restart` |
| диск | дані ≈ 50–70 MB/день; місяць ≈ 2 GB |

## Безпека / витрати
- Відкритий назовні потрібен тільки порт 22 (робить deploy.sh; в консолі — Security List за замовчуванням).
- A1.Flex 2/12 у межах Always Free (4 OCPU/24GB загалом). E2.1.Micro теж free. Монітор: Billing → Cost analysis.
- Рекодер read-only по ринках, жодних ключів бірж/гаманців на сервері.
