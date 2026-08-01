FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# API MAX (platform-api2.max.ru) подписан корневым сертификатом Минцифры РФ, которого
# нет ни в базовом образе, ни в наборе certifi: без него TLS падает с
# "unable to get local issuer certificate". Сертификаты публичные (gu-st.ru).
# Кладём в ДВА места: системное хранилище — для aiohttp/aiogram (берут системные CA),
# и bundle certifi — для httpx, который системные CA игнорирует и ходит со своим.
COPY certs/*.cer /usr/local/share/ca-certificates/
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && for f in /usr/local/share/ca-certificates/*.cer; do mv "$f" "${f%.cer}.crt"; done \
    && update-ca-certificates \
    && for f in /usr/local/share/ca-certificates/*.crt; do printf '\n' >> "$(python -c 'import certifi; print(certifi.where())')"; cat "$f" >> "$(python -c 'import certifi; print(certifi.where())')"; done \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app

CMD ["python", "-m", "app.main"]
