# Running Analytics

## Extração de atividades do Strava

1. Copie `.env.example` para `.env` e preencha `STRAVA_CLIENT_ID`,
   `STRAVA_CLIENT_SECRET`, `DATABASE_URL` e `POSTGRES_PASSWORD`. Mantenha o
   `.env` privado: nunca o inclua em commits.
2. Nas configurações do aplicativo da API do Strava, cadastre o domínio de
   redirecionamento `localhost`. O endereço de retorno padrão é
   `http://localhost/exchange_token`.
3. Instale as dependências do projeto:

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

   No DBeaver, execute `sql/schema.sql` se o banco estiver vazio. Se já criou o
   banco com o schema da etapa anterior, execute somente
   `sql/migrations/001_strava_extraction.sql` para adicionar as colunas de streams
   e o estado de sincronização.
4. Execute o fluxo OAuth uma vez:

   ```powershell
   .\.venv\Scripts\Activate.ps1
   python -m src.extract.strava --authorize
   ```

   Autorize o escopo solicitado (`activity:read_all`) e cole o endereço
   completo da barra do navegador quando solicitado. Os tokens de acesso e de
   atualização são salvos em `data/strava_tokens.json`, arquivo ignorado pelo Git.
5. Inicie a extração:

   ```powershell
   python -m src.extract.strava
   ```

Na primeira execução, o programa importa o histórico em páginas de até 200
atividades. Nas execuções seguintes, busca atividades mais recentes que a última
data salva, com sobreposição de um segundo e deduplicação pelo ID. Os pontos de
retomada do backfill e da carga incremental ficam em `strava_sync_state`, permitindo
repetir uma página após uma interrupção. O JSON original é armazenado em
`raw_strava_activities.payload`, sem modificações. Os streams ficam separados na
coluna `raw_strava_activities.streams`. Streams indisponíveis são marcados em
`streams_status` para nova tentativa na execução seguinte.

O cliente renova tokens OAuth expirados, valida o formato de atividades e streams,
repete tentativas em erros temporários e acompanha os cabeçalhos de limite da API
do Strava. Quando o limite diário de leitura é atingido, a execução para sem
avançar além de uma página incompleta. Execute-a novamente depois da renovação
do limite.

Consulte a documentação atual do Strava sobre [limites de requisição](https://developers.strava.com/docs/rate-limits/)
e o [Acordo da API](https://www.strava.com/legal/api).

Para executar os testes específicos do extrator:

```powershell
pytest tests/test_strava_extractor.py
```