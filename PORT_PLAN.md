# Prompt: OrcaSlicer MCP → ElegooSlicer (Centauri Carbon 2 + CANVAS)

> **Como usar:** crie o fork do ElegooSlicer (Fase 0 explica), coloque este arquivo na raiz do repositório como `PORT_PLAN.md`, abra o Claude Code ali e diga: *"Leia PORT_PLAN.md e execute a Fase 0."* Cada fase termina num ponto de parada; você revisa e manda seguir.
>
> Fatos verificados em 13/10/2026 contra o código-fonte (commits citados abaixo). O agente deve reconfirmar, porque os repositórios andam rápido.

---

## 1. Papel e objetivo

Você é o engenheiro responsável por portar a **Remote API do fork OrcaSlicer MCP** (MaxEllis) para o **ElegooSlicer**, e por estender essa API e o servidor MCP em Python para cobrir o que falta para a impressora do Victor: **Elegoo Centauri Carbon 2 Combo com CANVAS (4 cores)**.

Resultado final esperado:

1. Uma build própria do ElegooSlicer ("ElegooSlicer MCP") com a Remote API funcionando com paridade total com o fork `v2.4.2-mcp.13`, mantendo perfis oficiais, Device Assistant, CANVAS e estimativa de tempo da Elegoo.
2. Endpoints novos para impressoras Elegoo (status, CANVAS, mapeamento de filamentos, envio de impressão protegido), slots de filamento, partes (volumes), projeto e calibração.
3. Um fork do servidor `orcaslicer-mcp` com as ferramentas novas, testes, documentação e um fluxo seguro de "preparar → confirmar → imprimir".
4. CI que gera a build para Windows e um guia para rebasear quando a Elegoo lançar versão nova.

## 2. Contexto verificado

### 2.1 Servidor MCP (Python)
- Repo `github.com/MaxEllis/orcaslicer-mcp`, versão **0.1.17** (12/10/2026), **AGPL-3.0**, 53 tools, mais de 500 testes unitários (`uv run pytest`).
- Fala com o slicer em `http://127.0.0.1:13130/api/v1` com o header `X-Api-Token`. Cliente em `src/orcaslicer_mcp/client.py`, tools em `server.py`.
- Detecta capacidade por **404 de rota** (`_m4_err` em `server.py`), então endpoints novos não quebram builds antigas.
- As ferramentas de impressora (`printer/`) só falam Moonraker e OctoPrint, e são **somente leitura**. `printer/target.py` resolve a impressora pelo `host_type` do perfil. `elegoolink` não é tratado hoje.
- `guard.py` bloqueia chaves perigosas (`post_process`, `*_gcode`, `printer_model`, conexão). `scripts/extract_settings_schema.py` gera `data/print_settings_schema.json` a partir do `PrintConfig.cpp`.

### 2.2 Fork do slicer com a API (C++)
- Repo `github.com/MaxEllis/OrcaSlicer`, branch `remote-api-242`, tag **`v2.4.2-mcp.13` = `fa3c9ee`**.
- Base: tag upstream **`v2.4.2` = `8500fcd`**. A camada da API são os commits `e6cc428..fa3c9ee`.
- `git diff 8500fcd fa3c9ee` tem 44 arquivos. O que importa:
  - **Novos:** `src/slic3r/GUI/RemoteAPI/RemoteAPI{Config,Server,Controller}.{hpp,cpp}` (~2.900 linhas; Boost.Beast HTTP + WebSocket, nlohmann::json, OpenSSL `RAND_bytes` para o token).
  - **Ganchos em arquivos existentes** (~600 linhas): `GUI_App.{cpp,hpp}` (ciclo de vida, updater, versão), `Preferences.{cpp,hpp}` (aba "Remote API"), `Plater.{cpp,hpp}` (diálogo multipart, título, `notify_project_opened`, `stop_background_slicing`), `MainFrame.cpp` (exclusão mútua com o auto-backup), `Tab.cpp` (`notify_config_changed`), `GLCanvas3D.{cpp,hpp}` + `GCodeViewer.{cpp,hpp}` (render offscreen), `NotificationManager.hpp` (`RemoteAPIChange`), `Format/bbs_3mf.{cpp,hpp}` (`backup_defer`), `AboutDialog.cpp`, `libslic3r_version.h.in`, `version.inc`, `src/slic3r/CMakeLists.txt`, `build_release_vs.bat`.
  - **Não portar:** `.github/` do fork, `README.md`, `docs/research/`, `resources/images/splash_logo*.svg` (marca Orca) e a mudança em `tests/libslic3r/test_placeholder_parser.cpp`, que é específica da CI deles.
- Rotas existentes (dispatch em `RemoteAPIController.cpp`): `status`, `config` (GET/PUT), `slice`, `slice/status`, `slice/cancel`, `model`, `preset` (PUT/DELETE), `preset/save`, `preset/config`, `presets`, `gcode`, `objects` (+ `/{id}` DELETE, `transform`, `duplicate`, `plate`, `config`, `layer_height`, `height_range`), `plate/render`, `arrange`, `orient`, `jobs/status`, `plates` (+ select/delete), WebSocket `events`.
- Preferências usadas: `remote_api_enabled`, `remote_api_bind_lan`, `remote_api_notify`, `remote_api_allow_sensitive`, `remote_api_port`, `remote_api_token`.

### 2.3 ElegooSlicer
- Repo `github.com/elegooofficial/ElegooSlicer`, tag **`v1.5.3.5` = `3f1042e`**. `version.inc` traz `ORCA_COMPATIBLE_VERSION "2.4.2"`, `SLIC3R_APP_NAME "ElegooSlicer"` e `ELEGOOSLICER_VERSION`. **Não existe `SoftFever_VERSION`.**
- Todos os pontos de gancho do fork existem no ElegooSlicer. Os headers usados pelo controller são praticamente idênticos aos do Orca 2.4.2. As diferenças relevantes:
  - `load_bbs_3mf` ganhou o parâmetro `std::string& generator`;
  - `Sidebar::auto_calc_flushing_volumes_internal` ganhou `const DynamicPrintConfig&`;
  - `CalcFlushingVolume` ganhou `printer_settings_id` (com default);
  - `PresetBundle` tem `LoadMachineOnly`;
  - `Preset::is_elegoo_cc_printer()` existe.
- **Teste de aplicação já feito:** `git apply --reject` do diff acima sobre `v1.5.3.5`. Resultado: **10 hunks rejeitados em 6 arquivos**, todos de marca/versão/updater:
  - `GUI_App.cpp` (4: construtor, `check_new_version_sf`, `format_display_version`, bloco perto de `stop_sync_user_preset`);
  - `Plater.cpp` (2: títulos; o Elegoo usa `" - " + SLIC3R_APP_NAME`);
  - `GUI_App.hpp` (1: contexto do include `Elegoo/TelemetryEvents.hpp`);
  - `AboutDialog.cpp` (1), `libslic3r_version.h.in` (1), `version.inc` (1).
  - **O resto aplica limpo**, inclusive os arquivos novos, `Preferences.cpp`, `GLCanvas3D.cpp`, `GCodeViewer.cpp`, `MainFrame.cpp`, `Tab.cpp` e `bbs_3mf.cpp`.
- **Updater:** `GUI_App::check_new_version_sf` do Elegoo consulta servidores da Elegoo e ofereceria trocar a build pela oficial, o que remove a API. Ele precisa ser redirecionado, como o fork faz. O `PresetUpdater` de perfis da Elegoo pode ficar.
- **Chaves de config só no Elegoo:** `bed_texture_area`, `support_mms`, `support_wan_network`.
- **Pilha de dispositivos** (`src/slic3r/Utils/Elegoo/`):
  - `PrinterManager` (singleton): `getPrinterList`, `getPrinterNetworkInfo`, `getSelectedPrinter`, `getPrinterMmsInfo`, `getPrinterStatusRaw`, `getPrintTaskList`, `getExceptionList`, `getFileList`, `refreshPrinterStatus`. Status vem do `PrinterCache`, atualizado por eventos.
  - `PrinterMmsManager`: `getPrinterMmsInfo`, `getFilamentMmsMapping`.
  - `PrinterUploadManager`: `upload`, `startAsyncUpload`, `getUploadTask`, `cancelUploadTask`.
  - `ElegooLink` (wrapper) sobre o SDK **elegoo-link** (`github.com/ELEGOO-3D/elegoo-link`, **Apache-2.0**; MQTT/WebSocket/HTTP; adaptador `elegoo_fdm_cc2`). O SDK expõe `pausePrint`, `resumePrint`, `stopPrint`, `setAutoRefill` e `getCanvasStatus`, que o wrapper do ElegooSlicer **ainda não expõe**.
  - Structs e conversores JSON prontos em `src/libslic3r/PrinterNetworkInfo.hpp` (`PrinterNetworkInfo`, `PrinterMmsGroup/PrinterMms/PrinterMmsTray`, `PrintFilamentMmsMapping`, `PrinterNetworkParams`, `UploadTaskInfo`, `convert*ToJson`).
  - **Atenção:** `convertPrinterNetworkInfoToJson` inclui `password`, `token`, `accessCode` e `pinCode`.
  - `MultiInstanceCoordinator`: instâncias "slave" roteiam a rede via IPC para a "master".
- **Fluxo de envio da GUI** (o comportamento a reproduzir sem diálogos):
  1. `Plater::send_gcode_legacy` abre o `PrintSendDialogEx` (diálogo web modal). Ele preenche `extended_info` com `selectedPrinterId`, `bedType`, `timeLapse`, `heatedBedLeveling`, `autoRefill`, `hasMms` e `filamentAmsMapping` (JSON). A preparação do mapeamento está em `PrintSendDialogEx::preparePrintTask` e `getPrinterMmsInfo`.
  2. `p->export_gcode(..., upload_job)` exporta o arquivo.
  3. `PrintHostJobQueue::priv::perform_job` (`Utils/PrintHost.cpp`) monta `PrinterNetworkParams` (`btPC` → `bedType = 1`) e chama `PrinterUploadManager::upload`.
- **Sync do CANVAS na GUI:** `PlaterExt::sync_mms_filament` (`GUI/PlaterExt.cpp`) abre `PrinterMmsSyncView` (modal), converte bandejas em `filament_ams_list`, chama `preset_bundle->sync_ams_list(...)` e mostra `MessageDialog`s.
- **G-code do CANVAS:** `M6211`, vindo dos perfis `resources/profiles/Elegoo/machine/ECC2/`. A estimativa de tempo está em `libslic3r/GCode/ElegooGCodeProcessorHelper.cpp`.
- **Dependência `elegoolink`:** `deps/elegoolink/elegoolink.cmake` usa `GIT_TAG origin/main` (flutuante; o main está em `46c7b81` hoje). Recursos de nuvem dependem de um `.env` privado (`AGORA_APP_ID`, URLs). Builds próprias devem funcionar só em LAN.
- **Build Windows:** a CI (`.github/workflows/build_elegoo.yml` e `build_deps.yml`) usa `build_release_vs2022.bat` (deps) e `build_release_vs2022.bat slicer`.

### 2.4 Fatos da CC2 vindos da comunidade (confirmar no hardware)
Fonte: `github.com/bjan/pycentauri`, Apache-2.0.
- LAN via MQTT (porta 1883), usuário `elegoo` e o access code da tela.
- A API local exige o modo **"LAN Only"** ligado (firmware 2.0).
- Mais de 3 requisições MQTT seguidas disparam um cooldown silencioso de alguns segundos.
- Câmera MJPEG **sem autenticação** em `:8080` (a CC1 usa `:3031/video`).
- Sem descoberta por UDP: use IP fixo.

### 2.5 Ambiente do Victor
- Windows 11 Pro, Ryzen 9 5950X, 128 GB RAM, VS 2022.
- O SSD principal tem pouco espaço livre: **meça antes do build de deps** e avise se faltar.
- Também tem um MacBook Air M5 (build macOS é opcional).
- A impressora chega em **17/10/2026**. Até lá, tudo que depende de hardware precisa funcionar sem impressora: endpoints respondem `printer_not_found`/vazio, e os testes usam mocks.

## 3. Regras (não negociáveis)

1. **Código simples e legível.** O Victor precisa entender o que foi feito. Nada de abstrações genéricas, templates espertos ou frameworks novos. Funções diretas, nomes claros, comentários explicando o *porquê*. Prefira repetir 5 linhas a criar uma hierarquia de classes.
2. **Diff mínimo contra o ElegooSlicer.**
   - Código novo fica em `src/slic3r/GUI/RemoteAPI/`. Coisas específicas da Elegoo vão num arquivo próprio (ex.: `RemoteAPIElegoo.cpp`).
   - Ganchos em arquivos do Elegoo são pequenos e marcados com `// elegoo-mcp:`.
   - Ao substituir comportamento (updater), deixe o código original inalcançável em vez de apagar, como o fork faz, para facilitar rebases.
3. **Manter todas as garantias do fork:**
   - token obrigatório;
   - bind só em 127.0.0.1 por padrão;
   - política de chaves sensíveis, incluindo `support_mms` e `support_wan_network` como sensíveis;
   - **nenhum diálogo modal disparado pela API**;
   - `run_on_ui` com timeout;
   - exclusão mútua com o auto-backup;
   - notificações in-app.
4. **Rede nunca na thread da GUI.** Chamadas ao `PrinterManager`/`ElegooLink` rodam fora da thread de UI, com timeout, e respondem `504 printer_timeout` se estourar.
   - Leituras de status saem do `PrinterCache`.
   - `getPrinterStatusRaw` é limitado a 1 chamada a cada 2 s por impressora; dentro do intervalo, devolva a última resposta com `age_s`.
5. **Ações físicas** (enviar, iniciar, pausar, retomar, parar impressão):
   - desligadas por padrão, atrás de uma preferência nova `remote_api_allow_print_control` ("Allow sending and controlling prints");
   - no MCP, exigem confirmação explícita do usuário a cada impressão;
   - sempre geram notificação in-app;
   - **só enviam o G-code que o próprio slicer gerou para o plate atual.** Nunca aceite arquivo ou caminho arbitrário vindo do cliente, porque G-code pode danificar a impressora.
6. **Segredos nunca saem da API.** Ao serializar impressoras, use **lista de campos permitidos** (whitelist), não blacklist. `password`, `token`, `accessCode`, `pinCode` e `extraInfo` nunca aparecem. Nada de segredos em logs.
7. **Licenças:**
   - ElegooSlicer, o fork e o MCP são AGPL-3.0; o elegoo-link é Apache-2.0.
   - Preserve cabeçalhos e créditos.
   - O commit do port cita `MaxEllis/OrcaSlicer@fa3c9ee`.
   - Releases binárias publicam o código-fonte correspondente.
8. **Idiomas:** código, comentários, commits e docs em inglês (padrão upstream). Relatórios para o Victor em português do Brasil.
9. **Pare e pergunte antes de:**
   - force-push, ou apagar branch/tag;
   - publicar release;
   - qualquer teste que envie algo para a impressora real (o Victor precisa estar presente);
   - adicionar dependência nova;
   - mudar o formato de resposta de um endpoint existente.
10. **Continuidade entre sessões:** mantenha `PORT_NOTES.md` na raiz com o mapa de ganchos, decisões, desvios deste plano, estado de cada fase e comandos de build/teste que funcionaram.

## 4. Repositórios e branches

- **Slicer:** fork de `elegooofficial/ElegooSlicer` (sugestão: `shiftsad/ElegooSlicer-MCP`).
  - Branch `remote-api` a partir da tag `v1.5.3.5`.
  - Remotes: `elegoo` (upstream oficial) e `orcamcp` (`MaxEllis/OrcaSlicer`).
  - Tags de release: `v1.5.3.5-mcp.N` (N monotônico, como o `ORCA_MCP_RELEASE` do fork).
- **MCP:** fork de `MaxEllis/orcaslicer-mcp`, branch `elegoo`.
  - Mantenha o nome do módulo `orcaslicer_mcp` para facilitar merges futuros.
  - Instalação por `uvx --from git+https://github.com/<user>/orcaslicer-mcp@elegoo orcaslicer-mcp`. Não publicar no PyPI.
- Melhorias genéricas (volumes, slots de filamento, undo, calibração, salvar projeto) devem ficar em commits separados das específicas da Elegoo, para virar PR para o MaxEllis depois se o Victor quiser.

## 5. Fases

### Fase 0: Reconhecimento (sem alterar código)
1. Confirme os fatos da seção 2 (tags, SHAs, contagem de hunks rejeitados). Reproduza o teste:
   ```bash
   git diff 8500fcd fa3c9ee -- src version.inc build_release_vs.bat docs/remote-api > remote-api.patch
   git apply --check -v remote-api.patch   # no checkout v1.5.3.5
   ```
2. Monte em `PORT_NOTES.md` a tabela "gancho do fork → local no ElegooSlicer → como adaptar" para cada hunk (os 10 rejeitados com a resolução proposta).
3. Leia `RemoteAPIController.cpp` inteiro e liste toda chamada a API interna do slicer. Confirme a assinatura de cada uma no ElegooSlicer.
4. Meça o espaço em disco. Planeje o build (deps → slicer) e o tempo estimado.
5. **PARE.** Mande ao Victor um resumo curto em PT-BR: riscos, decisões pendentes, plano.

### Fase 1: Port 1:1 (paridade com `v2.4.2-mcp.13`)
1. Primeiro commit: aplicar o patch (excluindo o que a seção 2.2 manda não portar). Atribuição no corpo do commit. Resolva os 10 hunks:
   - Versão: crie `ELEGOO_MCP_RELEASE` em `version.inc` e `ELEGOO_MCP_VERSION = ELEGOOSLICER_VERSION "-mcp." N` em `libslic3r_version.h.in`.
   - `format_display_version()`, About e splash mostram `1.5.3.5-mcp.N`. Não substitua os SVGs da Elegoo; só garanta que a versão apareça.
   - Títulos: `" - " SLIC3R_APP_NAME " MCP"`.
   - `/status` passa a devolver `app` = `SLIC3R_APP_NAME`, `app_version` = `ELEGOOSLICER_VERSION`, mais `orca_compatible_version` e `mcp_build`.
   - Updater: `check_new_version_sf` consulta `api.github.com/repos/<user>/ElegooSlicer-MCP/releases/latest` e compara o ordinal `-mcp.N` (copie a lógica do fork, incluindo o tratamento numérico do `skip_version`). O código da Elegoo fica inalcançável. Mantenha o `return` de Linux que já existe.
2. Instância secundária: se a porta 13130 estiver ocupada (multi-instância), registre no log e mostre uma notificação in-app ("Remote API port in use"). Não deixe a instância travar.
3. Compile no Windows com o script do repo. Rode o app. Habilite a API em Preferências.
4. Testes de paridade:
   - `scripts/remote-api-runtime-check.py` do MCP contra a build;
   - smoke com o MCP **0.1.17 sem modificação**: `get_status`, `load_model`, `slice_and_wait`, `get_slice_breakdown`, `render_plate` (editor e preview), `list_plates`, `set_config`, `select_preset` e `describe_plate`, usando o perfil **Elegoo Centauri Carbon 2 0.4 nozzle**.
5. Verifique que `describe_plate`/`get_slice_breakdown` não quebram com `M6211`. Veja se o tempo de troca de filamento aparece no total e se falta em algum papel. Registre o resultado.
6. **PARE.** O Victor testa a build e confirma.

**Aceite:** todas as tools do MCP 0.1.17 funcionam; nenhum diálogo modal aparece via API; o updater não oferece a versão oficial; a build se identifica como MCP.

### Fase 2: Impressoras Elegoo, somente leitura (C++)
Endpoints da seção 6.1. Implemente em `RemoteAPIElegoo.cpp`, registrado no `dispatch` com um gancho mínimo. Adicione `"printers"` e `"canvas"` em `capabilities`.

**Aceite (sem impressora):** respostas vazias ou `printer_not_found` corretas.
**Aceite (com impressora, depois de 17/10):** status, tarefa atual, CANVAS com 4 bandejas, histórico. O JSON não contém nenhum segredo (teste automatizado que procura as chaves proibidas).

### Fase 3: Multimaterial no projeto (C++)
Endpoints da seção 6.2: slots de filamento, volumes/partes, split, sync do CANVAS sem diálogo e mapeamento.
- Refatore o núcleo de `PlaterExt::sync_mms_filament` numa função sem UI que devolve um relatório (sincronizados, desconhecidos). A versão da GUI passa a chamar essa função e continua mostrando os diálogos dela. Comportamento idêntico na GUI.
- Mesma coisa para a montagem do mapeamento de `PrintSendDialogEx::preparePrintTask`: extraia para um helper compartilhado. Não copie.

**Aceite:** o sync via API produz o mesmo `filament_presets`/`filament_colour` que o botão da GUI para o mesmo estado do CANVAS; volumes mostram e aceitam `extruder`, e a coluna de filamento da lista de objetos atualiza.

### Fase 4: Envio e controle de impressão (C++, protegido)
Endpoints da seção 6.3.
1. Preferência `remote_api_allow_print_control` na aba Remote API, desligada por padrão. O tooltip explica o risco.
2. Estenda o wrapper `ElegooLink` e o `PrinterManager` com `pausePrint`, `resumePrint` e `stopPrint`, seguindo o padrão dos métodos existentes (lock por impressora, `CHECK_INITIALIZED`, rota IPC para slave).
3. O arquivo enviado precisa ser **byte a byte igual** ao que o "Enviar" da GUI geraria para o mesmo plate, salvo timestamps e nome. Descubra o formato que a CC2 usa (`.gcode` ou `.gcode.3mf`, flag `use_3mf`, thumbnails) e reutilize o caminho de export do Plater, sem reimplementar.
   - Teste: exportar pela GUI e pela API e comparar.
4. **Teste com a impressora só com o Victor presente:** peça pequena (cubo de calibração), `prepare` → confirmação → upload → status → cancelar no primeiro layer.

### Fase 5: Qualidade de vida (C++)
Endpoints da seção 6.4: salvar projeto, undo/redo, mudanças por altura (troca de cor/pausa) e calibrações.
- Calibrações (`Plater::calib_*`) criam um projeto novo. Use `new_project(skip_confirm=true, silent=true)` **só** se o projeto atual não tiver mudanças não salvas; caso contrário devolva `409 unsaved_project`, a menos que venha `"discard_project": true`.

### Fase 6: Servidor MCP (Python)
Ferramentas da seção 7.
1. Backend `printer/elegoo.py` que usa os endpoints do slicer. `target.py` passa a reconhecer `host_type` `elegoolink` (ou a capability `printers` no `/status`) e roteia `get_printer_status`, `wait_for_printer`, `list_print_history` e `check_printer_match` para ele.
2. Fluxo de impressão em duas etapas com confirmação (seção 7.3).
3. Snapshot da câmera (seção 7.4).
4. Atualizações do servidor:
   - regenere o schema com `scripts/extract_settings_schema.py --src <ElegooSlicer>/src/libslic3r/PrintConfig.cpp`;
   - adicione `support_mms` e `support_wan_network` ao `guard.py`;
   - atualize `README`, a seção Security e o `server.json` com nome próprio;
   - gere o `.mcpb`.
5. Conhecimento: crie `knowledge/printers/elegoo-cc2-canvas.md` só com fatos tirados do código e dos perfis (purga/flush, torre de purga, `M6211`, lados A/B da mesa mapeados a partir dos perfis, LAN Only, rate limit). Nada de números inventados.
6. Testes: unitários com API mockada para cada tool nova, incluindo a recusa sem confirmação e a redação de segredos. Estenda `scripts/remote-api-runtime-check.py` com as rotas novas.

### Fase 7: Build, CI, releases e manutenção
1. Fixe o `elegoolink` num commit (troque `origin/main` pelo SHA) para ter builds reproduzíveis.
2. Workflow `build_elegoo_mcp.yml` derivado de `build_elegoo.yml`/`build_deps.yml`: `windows-latest` com cache de deps, artefato portable `.zip` e `SHA256SUMS`. macOS opcional.
3. Release `v1.5.3.5-mcp.N`: notas, link para o código-fonte, checksums. **Só publique depois de confirmar com o Victor.**
4. `UPSTREAM_SYNC.md`:
   - como mover para uma versão nova do Elegoo (fetch da tag, rebase, a lista dos 10 hunks como pontos de conflito);
   - refazer o diff de chaves do `PrintConfig.cpp`, regenerar o schema e rodar os testes;
   - como puxar correções novas da API do MaxEllis: `git log fa3c9ee..orcamcp/remote-api-242 -- src/slic3r/GUI/RemoteAPI`.

## 6. Especificação dos endpoints novos

Convenções do fork:
- tudo sob `/api/v1`, JSON;
- erros como `{"error":"snake_case", ...detalhes}`, com 400/403/404/409/422/504;
- mutações atômicas; trabalho de GUI via `run_on_ui`;
- índices de plate 0-based; slots de filamento 1-based (como na GUI e no campo `extruder`).

### 6.1 Impressoras (leitura)

| Método | Rota | Resposta / notas |
|---|---|---|
| GET | `/printers` | `{"printers":[Printer], "selected_id": str\|null}`. `selected_id` = `PrinterManager::getSelectedPrinter(printer_model, recent)`, a mesma impressora que o diálogo de envio pré-selecionaria. |
| GET | `/printers/{id}` | `Printer` |
| GET | `/printers/{id}/status[?raw=1]` | `Printer` + `exceptions:[{code,level,time}]`. Com `raw=1`, inclui `raw` (JSON de `getPrinterStatusRaw`) e `raw_age_s`, com rate limit. |
| GET | `/printers/{id}/canvas` | `Canvas` (de `PrinterMmsManager::getPrinterMmsInfo`). `409 canvas_unsupported` se a impressora não tiver multimaterial. |
| GET | `/printers/{id}/tasks?page=1&size=20` | Histórico (`getPrintTaskList`). |
| GET | `/printers/{id}/files?page=1&size=20` | Arquivos na impressora (`getFileList`). Opcional. |

**`Printer`** (whitelist):
- `id`, `name`, `model`, `vendor`, `host`, `port`, `network` (`lan`\|`wan`), `firmware`, `connected`;
- `status` (nome derivado do enum `PrinterStatus`: `idle`, `printing`, `paused`, `preheating`, `homing`, `auto_leveling`, `filament_operating`, `print_completed`, `error`, `offline`, …) e `status_code`;
- `task`: `{name, file, progress, current_s, total_s, estimated_s, begin_ts}` ou `null`;
- `supports`: `{canvas, timelapse, auto_leveling, bed_switching, auto_refill, filament_mapping}`.

**`Canvas`**:
- `connected`, `system`, `auto_refill`, `active: {unit, tray}`;
- `units: [{id, name, model, temperature_c, humidity, connected, trays: [Tray]}]`;
- `external_spool: Tray|null`.

**`Tray`**:
- `id`, `status` (`empty`\|`preloaded`\|`loaded`\|`unknown_filament`\|`error`);
- `type`, `name`, `vendor`, `color` (`#rrggbb`);
- `nozzle_temp: [min, max]`, `bed_temp: [min, max]`;
- `matched_preset`, `matched_alias`.

### 6.2 Projeto multimaterial

| Método | Rota | Corpo → resposta |
|---|---|---|
| GET | `/filaments` | `{"count": n, "max": m, "slots": [{"slot":1, "preset", "type", "color", "used_on_current_plate": bool}]}` |
| PUT | `/filaments` | `{"count": n}`, pelo mesmo caminho do botão +/− da sidebar. Remover slot usado por objeto/volume → `409 slot_in_use` (lista quem usa), a menos que venha `"force": true`. |
| PUT | `/filaments/{slot}` | `{"preset"?: str, "color"?: "#rrggbb"}`. Preset pelo mesmo caminho do combo da sidebar; cor via `filament_colour`; recalcula flush se `auto_calculate_flush` estiver ativo. `422 unknown_preset` / `incompatible_preset`. |
| GET | `/objects/{id}/volumes` | `[{"id", "index", "name", "type": "part\|modifier\|negative\|support_enforcer\|support_blocker\|text", "extruder": int (0 = herda), "size_mm", "config": {...}}]` |
| PUT | `/objects/{id}/volumes/{vid}/config` | Mesma semântica atômica e política de chaves de `PUT /objects/{id}/config`. Atualiza a coluna de filamento da lista de objetos (o fork já faz isso para objetos; replique). |
| POST | `/objects/{id}/split` | `{"to": "objects"\|"parts"}` via `Plater::split_object`/`split_volume`, sem diálogos. Devolve os ids novos. `422 not_splittable`. |
| POST | `/printers/{id}/canvas/sync` | `{"mode"?: "filament_and_color"\|"color_only"}` (default = preferência `sync_ams_filament_mode`). Devolve `{"synced": n, "slots": [...], "unknown": [{tray, type, reason}]}`. Sem `MessageDialog`. |
| GET | `/printers/{id}/canvas/mapping[?plate=i]` | `{"plate", "sliced", "ok", "filaments": [{"slot", "preset", "type", "color", "weight_g", "tray": {unit, tray, type, color}\|null, "issues": ["no_tray"\|"type_mismatch"\|"color_mismatch"\|"tray_not_ready"]}]}`. Usa o helper extraído de `preparePrintTask` + `getFilamentMmsMapping`, para ser idêntico à GUI. |

### 6.3 Envio e controle (exigem `remote_api_allow_print_control`; sem ela → `403 print_control_disabled` com `hint`)

| Método | Rota | Corpo → resposta |
|---|---|---|
| POST | `/printers/{id}/print/prepare` | Corpo: `{"plate"?: i, "bed_side"?: "A"\|"B", "leveling"?: bool, "timelapse"?: bool, "auto_refill"?: bool, "mapping"?: [{"slot", "unit", "tray"}]}`. Os defaults vêm das chaves `recent` usadas pelo `PrintSendDialogEx` (`printsend_*`). Valida: plate fatiado (`409 not_sliced`), impressora conectada e ociosa (`409 printer_busy` + status), CANVAS conectado se usar mais de 1 filamento (`409 canvas_disconnected`), mapeamento completo e compatível (`422 mapping_incomplete` com detalhes). Devolve `{"plan_id", "expires_in_s": 300, "summary": {printer, plate, file_name, estimated_time_s, filament_g, layers, bed_side, leveling, timelapse, auto_refill, mapping, warnings}}`. O plano fica preso a uma impressão digital do slice; se o plate for refatiado ou a config mudar, ele expira. |
| POST | `/printers/{id}/print` | `{"plan_id"}`. Exporta pelo caminho do Plater (seção 5, Fase 4.3) e chama `PrinterUploadManager::startAsyncUpload` com `uploadAndStartPrint = true`. Devolve `202 {"upload_id"}`. `410 plan_expired`. |
| GET | `/uploads/{upload_id}` | `{"status": "exporting\|uploading\|success\|failed\|cancelled", "progress", "bytes": [up, total], "message"}` |
| DELETE | `/uploads/{upload_id}` | Cancela. |
| POST | `/printers/{id}/pause` · `/resume` · `/stop` | `{"ok": true}`. `/stop` exige o corpo `{"confirm": "stop"}` (proteção contra chamada acidental). |

Todas as rotas desta seção geram notificação in-app com o nome da impressora e da peça, independente de `remote_api_notify`.

### 6.4 Qualidade de vida

| Método | Rota | Corpo → resposta |
|---|---|---|
| POST | `/project/save` | `{"path"?: str, "overwrite"?: bool}`. Sem `path`, usa o caminho atual (`409 project_unnamed` se não houver). Exige extensão `.3mf`. Não sobrescreve sem `overwrite`. |
| POST | `/undo` · `/redo` | `{"ok", "can_undo", "can_redo"}` |
| GET / POST / DELETE | `/plates/{i}/layer_changes` | Itens de `plates_custom_gcodes[i]`: `{"z", "type": "tool_change\|color_change\|pause", "slot"?, "message"?}`. Valida `z` dentro da altura do objeto e o slot existente. |
| POST | `/calibration` | `{"type": "flow_rate\|pressure_advance\|temperature\|retraction\|max_volumetric_speed\|vfa", "params": {...}, "discard_project"?: bool}`. Usa `Plater::calib_*`, sem fatiar. Devolve os objetos criados. |

## 7. Ferramentas MCP novas

Siga o estilo do servidor:
- docstrings que dizem quando usar, o que cada campo significa e qual build é exigida;
- erros em `{"error", "message", "hint"}`;
- cada tool com `title` e hints de read-only/destrutivo (exigência do diretório de conectores);
- regere o `lhm.plugin.json` com `scripts/gen_lhm_tools.py`.

### 7.1 Impressora e CANVAS
- `list_printers()`
- `get_canvas(printer=None)`: bandejas, com destaque para as não prontas.
- `sync_canvas_to_project(printer=None, mode=None)`
- `preview_filament_mapping(printer=None, plate=None)`: tabela slot → bandeja com problemas.
- `get_printer_status`, `wait_for_printer`, `list_print_history`, `check_printer_match`: ganham o backend Elegoo, com a mesma interface.

### 7.2 Projeto
- `list_filament_slots()`, `set_filament_slot(slot, preset=None, color=None)`, `set_filament_count(n, force=False)`
- `list_parts(object_id)`, `set_part_filament(object_id, part_id, slot)`, `split_object(object_id, to)`
- `add_layer_change(z, type, slot=None)`, `list_layer_changes()`, `remove_layer_change(z)`
- `save_project(path=None, overwrite=False)`, `undo()`, `redo()`
- `run_calibration(type, params, discard_project=False)`

### 7.3 Impressão (duas etapas)
- `prepare_print(...)` devolve um resumo legível em uma frase mais o `plan_id`. O docstring manda o modelo **mostrar o resumo e perguntar ao usuário**.
- `start_print(plan_id)`:
  - se o cliente suportar MCP elicitation (`ctx.elicit`), pede confirmação ao usuário com o resumo e só segue com "accept";
  - senão, exige `user_confirmed=True`, e o docstring diz que só pode ser `True` depois que o usuário confirmou na conversa.
  - Em ambos os casos, a preferência do slicer precisa estar ligada.
- `get_upload_status(upload_id)`, `cancel_upload(upload_id)`
- `pause_print()`, `resume_print()`, `stop_print(confirm)` (stop com a mesma confirmação do `start_print`).

### 7.4 Câmera
- `get_camera_snapshot(printer=None)` devolve `Image`.
- Lê o primeiro JPEG completo do MJPEG da impressora: host vindo de `/printers`; porta/caminho confirmados no hardware (CC2 `:8080`, CC1 `:3031/video`); timeout de 10 s; limite de 8 MB.
- Somente leitura e só em LAN. Se não encontrar, `unsupported` com explicação.
- Uso típico: `wait_for_printer(first_layer_done)` → snapshot → o modelo avalia a primeira camada.

## 8. Critérios de aceite finais (cenário real: capacete de cosplay)

1. `load_model` de uma parte do capacete exportada do Blender → `auto_orient` → `render_plate` → `check_placement` no perfil CC2 0.4.
2. `get_canvas` → `sync_canvas_to_project` → `set_part_filament` em duas partes → `slice_and_wait` → `get_slice_breakdown` mostra a purga → `preview_filament_mapping` sem problemas.
3. `prepare_print` → Victor confirma → `start_print` → `get_upload_status` até `success` → `wait_for_printer(first_layer_done)` → `get_camera_snapshot`.
4. Sem confirmação, `start_print` é recusado. Com a preferência desligada, `403`. Nenhum JSON contém segredos. Nenhum diálogo modal aparece.
5. A GUI continua idêntica para quem não usa a API: enviar, sincronizar CANVAS e Device Assistant funcionam como no ElegooSlicer oficial.
6. Todos os testes Python passam. `remote-api-runtime-check.py` passa. A build da CI sai verde.

## 9. Riscos conhecidos

- **O Elegoo lança versão a cada poucas semanas** (1.5.3.4 → 1.5.3.5): manter `UPSTREAM_SYNC.md` e o diff mínimo.
- **`elegoolink` flutuante:** fixar o SHA.
- **Sem o `.env` da Elegoo,** recursos de nuvem (login, vídeo Agora, WAN) não funcionam na build própria. Confirme cedo que a CC2 conecta só em LAN (access code, LAN Only) pelo Device Assistant da build.
- **Multi-instância:** a API só funciona na instância que conseguir a porta. Avise quando falhar.
- **Rate limit da CC2:** nunca faça polling agressivo; use o cache.
- **Formato e campos do envio** podem mudar entre versões do Elegoo: os testes de "arquivo idêntico à GUI" e "sync idêntico à GUI" pegam regressões.
- **Telemetria:** o ElegooSlicer tem telemetria (`TelemetryReporter`, aparentemente ligada ao login). Não adicione dados da API a ela. Documente o comportamento no README.
