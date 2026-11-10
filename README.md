# Garimpo de Instagram para o Claude

Uma ferramenta que você liga no seu Claude para achar no Instagram os posts que mais deram certo no seu nicho. O Claude compara cada post com o normal do próprio perfil, separa o que saiu da curva e monta um painel com link que abre no celular.

Funciona pela API oficial da Meta e fica no ar no Vercel, de graça. Você não precisa saber programar.

[![Publicar no Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2Fvancarlosreis%2Fgarimpo-instagram&project-name=garimpo-instagram&repository-name=garimpo-instagram&env=IG_GRAPH_TOKEN,IG_USER_ID,CONECTOR_SEGREDO&envDescription=A%20chave%20de%2060%20dias%20da%20Meta%2C%20o%20n%C3%BAmero%20da%20conta%20do%20Instagram%20%28come%C3%A7a%20com%201784%29%20e%20a%20senha%20do%20endere%C3%A7o%20%2840%20letras%20e%20n%C3%BAmeros%29.&envLink=https%3A%2F%2Fgithub.com%2Fvancarlosreis%2Fgarimpo-instagram%23as-tres-informacoes&stores=%5B%7B%22type%22%3A%22blob%22%2C%22access%22%3A%22private%22%7D%5D)

## Como instalar, em 4 partes

1. **Pegue a chave na Meta** (15 min). Siga as Partes 1 a 3 do [LEIA-ME](LEIA-ME.txt): criar o app, gerar a chave de 60 dias e descobrir o número da conta.
2. **Clique no botão "Deploy" acima** (5 min). Entre com o GitHub, confirme o armazenamento privado e preencha as três informações abaixo. Clique em Deploy.
3. **Monte o link da ferramenta**: `https://<seu-projeto>.vercel.app/mcp/<senha do endereço>`. Use o endereço curto que aparece em Domains, não o endereço longo de cada publicação.
4. **Ligue no Claude**: claude.ai → Configurações → Conectores → Adicionar conector personalizado, com esse link.

Para conferir, abra o mesmo link com `/uso` no final. Deve aparecer "Situação da Meta: Liberada".

<a id="as-tres-informacoes"></a>
## As três informações

O Vercel pede três informações antes de publicar:

| Nome | O que colar | De onde vem |
| --- | --- | --- |
| `IG_GRAPH_TOKEN` | A chave de 60 dias da Meta. Começa com `EAA`. | Graph API Explorer → Depurador de token → Estender token de acesso |
| `IG_USER_ID` | O número da conta do Instagram. Começa com `1784`. | Graph API Explorer, consulta `me/accounts?fields=name,instagram_business_account{id,username}` |
| `CONECTOR_SEGREDO` | Uma senha de 40 letras e números, inventada por você. Ela vira o final do endereço. | Um gerador de senhas |

**A chave é uma senha.** Nunca cole em conversa com o Claude nem em nenhum chat. Ela só vai do site da Meta para o Vercel.

## Renovar a chave (a cada ~60 dias)

1. Gere uma chave nova e estenda para 60 dias, como na instalação.
2. No Vercel, abra o projeto → Settings → Environment Variables → `IG_GRAPH_TOKEN` → Edit, e cole a chave nova.
3. Em Deployments, clique em ⋯ → Redeploy.

A página de uso avisa quando faltam 10 dias.

## Limites

- Só lê posts públicos de contas profissionais (Empresa ou Criador).
- Cerca de 200 buscas por hora e 30 hashtags novas por semana, limites da Meta.
- Métricas públicas apenas: curtidas, comentários e visualizações.
- Referência é inspiração: não copie arte nem texto e mantenha o crédito do perfil.

## Arquivos

- `api/index.py`: o servidor MCP, com as instruções para o Claude e as 4 ferramentas.
- `api/app_garimpo.py`: coleta, ranking pelo normal do perfil, cache e painel.
- `api/garimpo.py` e `api/painel.py`: chamadas à API da Meta e montagem do painel.
- `api/uso.py`: registro de uso e a página `/uso`.
- `LEIA-ME.txt`: passo a passo completo. `GUIA-DE-USO.txt`: como pedir garimpos no Claude.
