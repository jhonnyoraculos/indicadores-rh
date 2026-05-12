# indicadores-rh

Aplicativo Streamlit para acompanhamento de indicadores de RH.

## Banco de dados

O app salva somente em banco de dados.
Para funcionar, ele exige `DATABASE_URL` configurada no ambiente local ou nos Secrets do Streamlit.

Exemplo de Secrets:

```toml
DATABASE_URL = "postgresql://usuario:senha@host:5432/banco?sslmode=require"
```

Importante:
Substitua `usuario`, `senha`, `host` e `banco` pelos dados reais do seu Postgres. Se deixar o exemplo literal, o app nao consegue conectar.

O app faz uma migracao automatica unica do CSV antigo para a tabela do banco, caso o banco esteja vazio e o arquivo legado ainda exista.

## Streamlit Cloud

No app publicado, abra **Settings > Secrets** e adicione a `DATABASE_URL` do seu Postgres.
Depois reinicie o app para ele conectar no banco externo e manter os dados acessiveis de qualquer lugar.
