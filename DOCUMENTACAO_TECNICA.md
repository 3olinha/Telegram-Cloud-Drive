# Documentação Técnica Completa — Telegram Cloud Drive (`v0.14`)

> **Propósito deste documento:** Especificação arquitetural e referência técnica exata do software **Telegram Cloud Drive (`v0.14`)** (*By Bolinha*), projetada para permitir que outra IA ou engenheiro de software compreenda integralmente o código-fonte, o modelo de concorrência, o protocolo MTProto/Telethon, a persistência isolada por máquina e o pipeline de empacotamento/autoatualização.

---

## 1. Visão Geral e Stack Tecnológica

### 1.1. Objetivo do Sistema
O **Telegram Cloud Drive** é um aplicativo desktop nativo para Windows (10/11, 64-bit) que utiliza grupos, canais ou o chat *"Mensagens Salvas"* (*Saved Messages*) do Telegram como backend de armazenamento em nuvem estilo Google Drive. Ele oferece upload em lote de múltiplos diretórios com motor paralelo multi-conexão, varredura incremental em segundo plano de grupos com dezenas de milhares de arquivos (60.000+), busca instantânea, download com retomada, exclusão sincronizada (local + nuvem), recuperação de estado pós-fechamento e autoatualização via GitHub Releases.

### 1.2. Stack, Linguagem e Bibliotecas Principais
- **Linguagem & Runtime:** Python `3.12.16` (CPython 64-bit para Windows), gerenciado via `uv` / `.venv`.
- **Interface Gráfica (GUI):**
  - `customtkinter` (`ctk`): Widgets modernos em tema escuro (*Dark/Blue*).
  - `tkinter` + `tkinter.ttk` (`ttk.Treeview`, `tk.Canvas`): Tabelas de alta capacidade para listagem de dezenas de milhares de arquivos, wallpaper vetorial customizado do Telegram e barras de progresso customizadas (`LabeledProgressBar`).
- **Comunicação com o Telegram:**
  - `telethon` (`v1.45.0`): Cliente MTProto assíncrono completo (`TelegramClient`, `MTProtoSender`, `SaveBigFilePartRequest`, `SaveFilePartRequest`, `InputMessagesFilterDocument`).
  - `cryptg` (`cryptg.cp312-win_amd64.pyd`): Extensão nativa compilada em C/Rust que acelera a criptografia/descriptografia `AES-256-IGE` do protocolo MTProto (eliminando o gargalo de CPU do `pyaes` puro em Python).
  - `urllib.request` (stdlib): Usado como fallback para envio HTTP `multipart/form-data` via Bot API (`https://api.telegram.org/bot<TOKEN>/sendDocument`) e para consultas REST à API do GitHub Releases.
- **Utilitários Gráficos & Sistema:**
  - `Pillow` (`PIL.Image`, `PIL.ImageDraw`): Renderização anti-aliased (4x supersampling `128x128 -> 28x28` com filtro `LANCZOS`) dos ícones da barra de navegação e exibição do QR Code.
  - `qrcode`: Geração local de QR Code para autenticação OAuth/MTProto inline (`client.qr_login()`).
  - `winreg`, `ctypes`, `faulthandler`, `hashlib` (stdlib): Fingerprinting de hardware do Windows (`MachineGuid`), registro do desinstalador no Painel de Controle (`HKCU\...\Uninstall\TelegramCloudDrive`), `AppUserModelID` da barra de tarefas e captura de *faults* nativos em `crash_trace.log`.
- **Empacotamento e Distribuição:**
  - `PyInstaller` (`v6.22.3`, modo `--onefile --windowed`): Compila tanto o executável principal portátil (`Telegram_Cloud_Drive_v0.14.exe`) quanto o instalador automatizado para Windows (`Instalador_Telegram_Cloud_Drive_v0.14.exe`).

### 1.3. Estrutura de Pastas e Arquivos
```text
C:\Users\tiago\.gemini\antigravity\scratch\Telegram_Cloud_Drive_Source\
├── main.py                          # Monolito principal da aplicação (~6.460 linhas): GUI, AsyncWorker, Upload/Download Turbo, Auto-Updater e Desinstalador (--uninstall)
├── installer_setup.py               # Código-fonte do Instalador do Windows (extrai payload_app.exe, cria atalhos .lnk e registra no Painel de Controle via winreg)
├── publicar_atualizacao_github.py   # Script de CI/CD local que sincroniza o código-fonte (.py) e publica Releases/Assets na API do GitHub
├── pyproject.toml / uv.lock         # Manifesto de dependências Python (customtkinter, pillow, qrcode, telethon, cryptg, pyinstaller)
├── telegram_icon.ico / .png         # Ícone oficial principal do aplicativo
├── bolinha.ico / .png               # Ícone secundário/legado
└── dist/                            # Saída dos binários compilados pelo PyInstaller

C:\Users\tiago\OneDrive - Sistema FIEG\Área de Trabalho\
├── Instalador\
│   └── Instalador_Telegram_Cloud_Drive_v0.14.exe   # Instalador standalone para Windows
└── Portatil\
    ├── Telegram_Cloud_Drive_v0.14.exe              # Executável portátil (roda direto do pendrive)
    └── COMO_USAR_VERSAO_PORTATIL.txt               # Manual do usuário da versão portátil
```

### 1.4. Fluxo Geral de Execução e Modelo de Concorrência (Dual-Thread)
O Tkinter/CustomTkinter não é *thread-safe* no Windows 11 (chamadas diretas ao interpretador Tcl/Tk a partir de threads secundárias causam *access violation* ou travamento silencioso). Por outro lado, o `Telethon` é 100% baseado em `asyncio`. Para isolar ambos:
1. **Thread Principal (Main UI Thread):** Executa o loop de eventos do Tkinter (`app.mainloop()`).
2. **Fila Thread-Safe de Despacho de UI (`self._ui_dispatch_queue`):**
   - `TelegramUploaderApp` sobrescreve o método `after(ms, func, *args)`.
   - Sempre que `self.after(0, callback)` é chamado por qualquer thread cujo `threading.get_ident() != self._main_thread_id`, a função **não** toca no Tcl/Tk; ela é colocada em `self._ui_dispatch_queue` (`queue.Queue`).
   - Na thread principal, o método `_drain_ui_dispatch_queue()` roda a cada `25 ms` via `super().after(25, ...)` e executa até 60 callbacks enfileirados por ciclo com tratamento de exceção individual.
3. **Thread Assíncrona Dedicada (`AsyncWorker`):**
   - Cria um `asyncio.new_event_loop()` exclusivo em uma daemon thread separada (`self.worker`).
   - Todas as corrotinas do Telethon são submetidas via `self.worker.run_coro(coro, callback, error_callback)` usando `asyncio.run_coroutine_threadsafe(coro, self.loop)`.
4. **Threads de I/O de Disco (`threading.Thread`):**
   - Varreduras de diretórios locais (`os.walk` / `os.scandir`), downloads HTTP de atualização e gravação de cache JSON grande (`group_cloud_cache.json`) rodam em daemon threads curtas para nunca bloquear nem o Tkinter nem o event loop do Telethon.

---

## 2. Integração com a API do Telegram

### 2.1. Modos de Operação: Client API (MTProto) vs. Bot API (HTTP)
O software suporta dois modos (`self.connected_mode`), sendo o **Modo Conta Pessoal (`personal`)** o principal:
1. **Conta Pessoal via MTProto (`Telethon`):**
   - Conecta diretamente aos Data Centers (DCs) do Telegram usando `API_ID` e `API_HASH` (por padrão `DEFAULT_API_ID = 2040`, `DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"` — credenciais oficiais públicas do Telegram Desktop, editáveis na aba `⚙ Opções > Informações da Conta`).
   - Permite arquivos de até **2.000 MiB (~2 GB)**, listagem completa de grupos/canais (`iter_dialogs`), leitura do histórico de arquivos (`iter_messages`), download e exclusão de mensagens (`delete_messages`).
2. **Modo Bot via HTTP (`_upload_via_bot_http`):**
   - Modo opcional (limitado pela API HTTP padrão de Bots a **50 MB** por arquivo) que faz `POST` multipart para `https://api.telegram.org/bot<TOKEN>/sendDocument`.

### 2.2. Autenticação da Conta Pessoal
Implementada na seção **`CONTA DO TELEGRAM`** (`_build_account_tab`), oferecendo dois fluxos inline (sem abrir janelas externas):
- **Login via QR Code (`start_qr_login` / `_async_qr_login_flow`):**
  - Chama `qr_login = await self.client.qr_login()`, gera a imagem QR com a biblioteca `qrcode` e exibe dentro do próprio painel (`self.qr_image_label`), aguardando `await qr_login.wait(timeout=120)`.
- **Login via Número de Telefone (`send_phone_code` / `confirm_inline_phone_code`):**
  - Combina o seletor de código de país (`COUNTRY_CODES`, ex: `+55`) com o número nacional, chama `client.send_code_request(phone)` e valida o código no campo inline via `client.sign_in(phone, code, phone_code_hash)`.
- **Suporte a 2FA (`SessionPasswordNeededError`):**
  - Caso a conta possua verificação em duas etapas, captura `errors.SessionPasswordNeededError` e autentica usando a senha informada em `self.password_2fa_entry` via `await self.client.sign_in(password=pwd)`.

### 2.3. Motor Turbo de Upload Paralelo e Chunking (`_fast_upload_file_telethon`)
O método padrão `client.upload_file()` do Telethon envia blocos de forma **sequencial** em uma única conexão TCP (envia 512 KB -> aguarda ~150–240 ms de RTT -> envia próximos 512 KB), o que limitava a taxa de upload a ~2 MB/s.
Para maximizar a velocidade na `v0.12+`:
1. **Pool de Conexões MTProto Paralelas (`_get_parallel_upload_senders`):**
   - Como a chave de autorização `self.client.session.auth_key` já é válida no Data Center primário da conta (`dc_id = self.client.session.dc_id`), o programa instancia até **4 conexões `MTProtoSender(auth_key)` adicionais** conectadas ao mesmo DC e inicializadas com `InvokeWithLayerRequest(LAYER, InitConnectionRequest(..., query=GetConfigRequest()))`.
   - Essas conexões ficam em cache (`self._parallel_upload_senders`) e são reutilizadas entre todos os arquivos da fila.
2. **Divisão em Blocos Máximos de 512 KB e Pipeline Concorrente (`16 workers`):**
   - O arquivo é dividido em partes de **`512 * 1024` bytes (512 KB)** — o maior bloco permitido pelo protocolo MTProto.
   - Arquivos `> 10 MB` usam `SaveBigFilePartRequest(file_id, part_idx, part_count, part_bytes)` e retornam um handle `InputFileBig(file_id, part_count, file_name)`.
   - Arquivos `<= 10 MB` calculam o hash MD5 sequencialmente na corrotina produtora (`_producer`), usam `SaveFilePartRequest(file_id, part_idx, part_bytes)` e retornam `InputSizedFile(file_id, part_count, file_name, md5=hash_md5, size=file_size)`.
   - Uma fila assíncrona limitada (`asyncio.Queue(maxsize=32)`) alimenta até **16 workers concorrentes** (4 requisições em voo simultâneas por conexão TCP, totalizando 8 MB em trânsito simultâneo).
   - Se qualquer conexão secundária sofrer queda momentânea em um bloco, o worker faz fallback transparente daquele único bloco de 512 KB para a conexão principal (`await self.client(req)`).
3. **Finalização da Mensagem de Mídia:**
   - Após o upload paralelo concluir o handle `InputFileBig` / `InputSizedFile`, o programa extrai os atributos/MIME via `utils.get_attributes(file_str, force_document=force_doc)` e envia a referência ao grupo com `await self.client.send_file(entity=target_entity, file=uploaded_handle, force_document=force_doc, attributes=attributes, mime_type=mime_type)`.

### 2.4. Limites de Tamanho, Rate Limits e Política de Retentativa (10s x 10 + Ciclo de 1 Hora)
- **Filtro Automático de Arquivos `> 2 GB` (`MAX_TELEGRAM_FILE_SIZE = 2000 * 1024 * 1024`):**
  - O protocolo do Telegram para contas padrão permite no máximo `4.000` partes de `512 KB` (`2.000 MiB = 2.097.152.000 bytes`).
  - Qualquer arquivo com `size > MAX_TELEGRAM_FILE_SIZE` é automaticamente ignorado durante a leitura de pastas (`_insert_scanned_batches`), na restauração de fila (`_restore_saved_queue_state`) e no loop de upload (`_async_process_queue`), recebendo status `"Ignorado (> 2GB)"`.
- **Tratamento de `FloodWaitError`:**
  - Se o servidor do Telegram retornar `errors.FloodWaitError`, o envio/download pausa exatamente por `fw.seconds` segundos usando `_async_sleep_interruptible_ul` / `_dl`, exibindo contagem regressiva na barra inferior.
- **Resiliência a Falhas de Rede (10 tentativas de 10s + Adiamento de 1 Hora):**
  - Em caso de queda de internet ou erro de socket, o arquivo atual tenta reconectar e reenviar até **10 vezes**, aguardando **10 segundos** entre tentativas.
  - Se falhar nas 10 tentativas, o arquivo recebe status `"Aguardando 1h"`, é colocado na lista `deferred_for_1h` e o programa avança imediatamente para o próximo arquivo da fila.
  - Ao terminar os demais arquivos da fila, o programa aguarda **1 hora (3600s)** com contador regressivo interrompível e inicia um novo ciclo para os arquivos adiados, repetindo até concluir ou o usuário clicar em `■ Parar`.

### 2.5. Indexação, Metadados e Cache Incremental de Grupos com 60.000+ Arquivos
- **Estrutura `RemoteCloudFile`:** Armazena `row_id` (`"dl_<message_id>"`), `message_id`, `filename`, `ext`, `size`, `date_str`, `caption`, `status`, `progress`, `checked` e `message_obj`.
- **Varredura Filtrada no Servidor (`InputMessagesFilterDocument`):**
  - `_async_scan_group_files` utiliza `self.client.iter_messages(target_entity, limit=None, filter=InputMessagesFilterDocument)`, fazendo o servidor do Telegram retornar apenas mensagens contendo documentos/arquivos (ignorando mensagens de texto puro).
- **Cache Incremental em Disco (`group_cloud_cache.json`):**
  - Para suportar grupos com mais de 60.000 arquivos sem reescanear do zero a cada abertura, `_save_group_cache_entry` grava no disco local (`%LOCALAPPDATA%\BolinhaTelegramUploader\pc_<hash>\group_cloud_cache.json`) os metadados de todos os arquivos já lidos de cada grupo, junto com `min_id`, `max_id` e `fully_scanned`.
  - Ao abrir o programa ou trocar de grupo, `scan_current_group_files` carrega os 60.000+ registros do cache local em **menos de 0,2 segundo**, indexa em `self.remote_by_id` e `self.remote_by_filename_lower` (dicionário hash $O(1)$ para checagem instantânea de arquivos repetidos) e renderiza a tabela em lotes não-bloqueantes de 400 linhas (`_render_dl_rows_chunked`).
  - Em seguida, em **segundo plano** (sem bloquear os botões de Upload ou Download):
    1. **Fase 1:** Busca apenas arquivos novos enviados após a última leitura (`min_id=max_cached_id`).
    2. **Fase 2:** Se uma varredura inicial gigante anterior foi interrompida antes do fim (`fully_scanned == False`), continua exatamente de onde parou (`max_id=min_cached_id - 1`), salvando checkpoints no disco a cada 1.000 arquivos lidos.
  - Quando o usuário solicita o download de um arquivo carregado do cache (`item.message_obj is None`), `_async_process_downloads` busca a mensagem sob demanda via `await self.client.get_messages(target_entity, ids=item.message_id)` antes de chamar `download_media`.

---

## 3. Interface, Operações e Persistência Local

### 3.1. Organização da Interface Gráfica
A janela principal (`TelegramUploaderApp`) é dividida em 5 faixas verticais:
1. **Cabeçalho Superior:** Logotipo, título (`Telegram Cloud Drive v0.14`), subtítulo e botões rápidos (`🔄 Atualizar`, `? Como Usar / Ajuda`, `⚙ Opções`).
2. **Barra de Conexão e Grupo Ativo:** Badge de status (`● Conectado: <Nome do Usuário>`), seletor destacado `Grupo / Chat Ativo` (`CTkComboBox` sem exibir IDs técnicos `[numero]`) e botão `↻ Atualizar Grupos`.
3. **Barra de Navegação Retangular (4 Abas):**
   - `UPLOAD PARA TELEGRAM` (ícone seta verde para cima)
   - `DOWNLOAD DO TELEGRAM (N)` (ícone seta azul para baixo + contador real de arquivos no grupo)
   - `HISTÓRICO DE ENVIADOS (N)` (ícone relógio âmbar)
   - `CONTA DO TELEGRAM` (ícone avião do Telegram)
4. **Área Central de Conteúdo (`self.pages_container`):**
   - **Aba Upload:**
     - Topo: Botão único `+ Selecionar Arquivos` (abre seletor unificado para *Arquivos Soltos*, *Escolher Pasta* ou *Selecionar Diretório com Várias Pastas*), checkboxes `Subpastas` e `Marcar já enviados`, filtro compacto `Exibir: [Todos | Pendentes]`, botão `Limpar Tudo` e resumo compacto (`N arq. (XX GB) | Pend: P`).
     - Centro: Tabela `ttk.Treeview` (`Segoe UI 12`, `rowheight=32`) com coluna de seleção `☑/☐` e canvas de fundo temático do Telegram (`draw_telegram_wallpaper_canvas`). Durante varredura de pastas locais gigantes (`self.is_scanning`), exibe o overlay central `"⏳ Lendo os arquivos, aguarde..."` e deixa o botão Iniciar cinza até concluir a leitura local.
     - Rodapé da aba: Checkbox `Enviar como Documento (sem compressão)`, botão `🗑 Excluir Selecionado`, botão dinâmico unificado **Iniciar/Pausar** (**Verde** `"▶ INICIAR UPLOAD"` / `"▶ CONTINUAR UPLOAD"` quando aguardando ou pausado; **Amarelo** `"⏸ PAUSAR UPLOAD"` durante o envio) e botão `■ Parar`.
   - **Aba Download:**
     - Topo: Barra de busca em tempo real (`self.search_var` filtrando por nome, extensão, legenda ou data), botão `Limpar Busca`, botão `🔄 Escanear Arquivos do Grupo` e resumo de arquivos.
     - Centro: Tabela `ttk.Treeview` (`Segoe UI 12`) com seleção `☑/☐`.
     - Rodapé da aba: Botão `📁 Escolher Pasta para Salvar`, campo de diretório, `📂 Abrir Pasta`, `Selecionar Todos`, `🗑 Excluir Selecionado`, botão unificado Verde/Amarelo `▶ BAIXAR SELECIONADOS` / `⏸ PAUSAR DOWNLOAD` e `■ Parar`.
5. **Rodapé Global de Progresso e ETA:**
   - Exibe o arquivo atual, velocidade em `MB/s` e duas barras `LabeledProgressBar` (`Progresso do Arquivo Atual` e `Progresso Total da Operação`) que desenham no centro da barra o percentual (`0.0%`–`100.0%`) e o **Tempo Previsto para a Conclusão (ETA)** (`format_eta`, calculado com base nos bytes restantes do arquivo atual e da fila total).

### 3.2. Operações Especiais de Exclusão e Duplicatas
- **Verificação de Duplicatas com Contagem Regressiva de 10s (`_ask_duplicate_upload_10s`):**
  - Se a opção `Ignorar repetidos` (em `⚙ Opções`) estiver ativa, antes de subir um arquivo o programa consulta `_file_exists_in_download_scan(item)`.
  - Se já existir no grupo ou no histórico, abre um modal com barra regressiva de **10 segundos** e 3 botões:
    - `✅ SIM (Subir)`: Envia o arquivo novamente.
    - `❌ NÃO (Ignorar — 10s)`: Ignora o arquivo atual (ação padrão caso o tempo esgote sem clique).
    - `⏭ IGNORAR TODOS`: Marca todos os arquivos repetidos restantes da fila como `"Ignorado (Repetido)"` de uma só vez e envia apenas os arquivos inéditos.
- **Exclusão na Aba Upload (`remove_selected_items`):**
  - Remove os itens marcados (`☑`) ou selecionados da tabela e das estruturas `self.queue`, `self.queue_by_id` e `self.queued_paths`, marcando `item.ignored_by_delete = True`.
  - Se o arquivo excluído estiver sendo enviado naquele exato momento, o callback `progress_cb` dispara `ItemDeletedFromQueueError`, abortando imediatamente o envio daquele arquivo e passando para o próximo.
- **Exclusão Sincronizada na Aba Download (`remove_selected_download_items`):**
  - Pede confirmação ao usuário, remove os itens da tabela local e do cache, e executa `_async_delete_messages_from_group`, que chama `await self.client.delete_messages(target_entity, chunk_ids, revoke=True)` em lotes de 100 IDs para **apagar os arquivos também de dentro do grupo do Telegram**.

### 3.3. Persistência Local e Recuperação de Estado (Auto-Recovery)
Todos os dados de estado ficam isolados fora da pasta do executável, dentro de:
`%LOCALAPPDATA%\BolinhaTelegramUploader\pc_<16_chars_sha256_fingerprint>\`
- `session_personal.session`: Banco SQLite do Telethon contendo a chave de autenticação MTProto da sessão.
- `config.json`: Preferências do usuário (`api_id`, `api_hash`, `last_target`, `download_dir`, `skip_already_sent`, `ignore_duplicates_prompt`, `hide_welcome_guide`, etc.).
- `sent_history.json`: Histórico persistente de todos os arquivos enviados (`signature = "<filename_lower>::<size>"`, data/hora, destino e caminho).
- `queue_state.json`: Snapshot completo da fila de upload (`items` + flag `was_uploading`). É salvo automaticamente a cada alteração na fila, a cada arquivo concluído e ao fechar o programa (`_on_app_closing`). Se o programa fechar inesperadamente no meio de um upload (`was_uploading == True`), ao reabrir ele restaura toda a fila (`_restore_saved_queue_state`), reconecta ao Telegram e **retoma o envio automaticamente** (`_auto_resume_interrupted_upload`).
- `group_cloud_cache.json`: Cache incremental dos arquivos escaneados de cada grupo do Telegram.
- `app_activity.log` e `crash_trace.log`: Logs persistentes de todas as operações, exceções Python (`sys.excepthook`, `threading.excepthook`, `report_callback_exception`) e falhas críticas de nível C (`faulthandler`).

---

## 4. Segurança, Desinstalação e Preparação para Distribuição

### 4.1. Isolamento de Sessão por Máquina (`get_machine_fingerprint`)
Um requisito crítico da versão portátil (pendrive) é garantir que, se o usuário copiar a pasta do programa para outra pessoa ou plugar o pendrive em outro computador, a conta do Telegram dele **não fique logada no outro PC**:
- `get_machine_fingerprint()` combina `platform.node()`, `USERNAME`, `COMPUTERNAME` e o `MachineGuid` do registro do Windows (`HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid`), gerando um hash SHA-256 de 16 caracteres (`pc_<hash>`).
- Os arquivos de sessão e configuração nunca são salvos na pasta do pendrive (`EXE_DIR`), mas sim em `%LOCALAPPDATA%\BolinhaTelegramUploader\pc_<hash>`. Assim, ao abrir em qualquer outro computador, a pasta local estará vazia e o programa solicitará um novo login.

### 4.2. Proteção de Credenciais de Publicação (`GITHUB_TOKEN`)
- O Personal Access Token do GitHub (`GITHUB_TOKEN`) usado para publicar novas versões fica armazenado **exclusivamente** no arquivo local `C:\Users\tiago\.env` da máquina do desenvolvedor.
- Ele é lido apenas pelo script de build/deploy `publicar_atualizacao_github.py` e **nunca é incluído ou empacotado** dentro de `main.py`, `installer_setup.py` ou dos executáveis `.exe` distribuídos.
- Os clientes finais consultam apenas o endpoint público de leitura `https://api.github.com/repos/3olinha/Telegram-Cloud-Drive/releases/latest` (sem necessidade de autenticação).

### 4.3. Instalador do Windows e Desinstalador Completo (`--uninstall`)
- **Instalador (`installer_setup.py` -> `Instalador_Telegram_Cloud_Drive_v0.14.exe`):**
  - Extrai o executável principal para `%LOCALAPPDATA%\Programs\TelegramCloudDrive\Telegram_Cloud_Drive.exe` (não requer privilégios de Administrador/UAC).
  - Cria atalhos `.lnk` na Área de Trabalho e no Menu Iniciar via Windows Script Host (`WScript.Shell`).
  - Registra o desinstalador no Painel de Controle do Windows (*Programas e Recursos* / *Aplicativos Instalados*) na chave `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\TelegramCloudDrive` com `UninstallString = '"...\Telegram_Cloud_Drive.exe" --uninstall'`.
- **Desinstalador (`run_uninstaller_flow` em `main.py`):**
  - Pode ser acionado pelo **Painel de Controle do Windows**, pelo atalho *Desinstalar* no Menu Iniciar ou pelo botão vermelho **`🗑 Desinstalar Programa e Apagar Dados de Login do Telegram`** dentro do menu `⚙ Opções`.
  - Ao confirmar, executa `await client.log_out()` para revogar a sessão no servidor do Telegram, fecha os descritores de arquivo, apaga integralmente `%LOCALAPPDATA%\BolinhaTelegramUploader` (forçando novo login caso o programa seja reinstalado no futuro), remove os atalhos `.lnk`, exclui a chave de registro `Uninstall` e agenda a remoção da pasta `%LOCALAPPDATA%\Programs\TelegramCloudDrive`.

---

## 5. Sistema de Autoatualização via GitHub Releases

### 5.1. Como o Antigravity Publica Novas Versões (`publicar_atualizacao_github.py`)
Para evitar falsos positivos do filtro anti-abuso do GitHub (que suspende contas que hospedam binários `.exe` em repositórios sem código-fonte ou com scripts `.bat`/PowerShell suspeitos), o pipeline de publicação executa 5 etapas via GitHub REST API:
1. **Sincronização do Código-Fonte (`sync_source_code_to_github`):** Faz commit/update dos arquivos `.gitignore`, `pyproject.toml`, `main.py` e `installer_setup.py` na branch `main` do repositório `3olinha/Telegram-Cloud-Drive`, mantendo o repositório classificado como `100% Python` com histórico de commits real.
2. **Criação/Atualização da Release (`vX.XX`):** Cria ou atualiza a release correspondente à tag `v{APP_VERSION}` com changelog detalhado e links diretos.
3. **Upload Ordenado dos 3 Assets:**
   - `Telegram_Cloud_Drive_v{version}.exe` (Executável portátil / aplicativo principal).
   - `Windows_Instalador_Telegram_Cloud_Drive_v{version}.exe` (Instalador do Windows — prefixado com **`W`** para que, na ordenação alfabética da API do GitHub, `Telegram_Cloud_Drive...` com letra **`T`** seja sempre `assets[0]`, garantindo retrocompatibilidade com versões antigas do auto-updater).
   - `Telegram_Cloud_Drive_v{version}_Portatil.zip` (Pacote `.zip` contendo apenas o `.exe` portátil e `COMO_USAR_VERSAO_PORTATIL.txt`, sem nenhum arquivo `.bat`).
4. **Atualização do `README.md`:** Atualiza a página inicial do repositório com os botões diretos para baixar o Instalador ou a Versão Portátil.

### 5.2. Como o Cliente Verifica, Baixa e Aplica a Atualização (`check_for_updates`)
1. **Verificação (`check_for_updates`):**
   - Executada automaticamente `1,8s` após iniciar o programa (`silent_if_up_to_date=True`) ou manualmente pelo botão `🔄 Atualizar`.
   - Faz `GET` em `https://api.github.com/repos/3olinha/Telegram-Cloud-Drive/releases/latest`, converte `tag_name` e `APP_VERSION` em tuplas numéricas via `parse_version_tuple` (ex: `"v0.14" -> (0, 14)`).
   - Seleciona o asset `.exe` principal ignorando qualquer arquivo cujo nome contenha `"instalador"`, `"setup"`, `"uninst"` ou `"desinstal"`.
2. **Prompt ao Usuário (`_show_update_available_dialog`):**
   - Se `remote_tuple > local_tuple`, exibe a janela modal **"🚀 Nova Atualização Disponível!"** com as notas da versão e os botões **`✓ SIM, ATUALIZAR AGORA`** e **`✕ NÃO, DEPOIS`**.
3. **Download e Substituição Limpa sem Conflito de `_MEI` (`_start_auto_update_download` & `_launch_self_updater_and_exit`):**
   - Ao clicar em **SIM**, baixa o novo `.exe` em blocos de 64 KB para `_downloading_Telegram_Cloud_Drive_vX.XX.exe.part` atualizando a `LabeledProgressBar`, renomeia para `Telegram_Cloud_Drive_vX.XX.exe` e remove o fluxo NTFS `:Zone.Identifier` via `os.unlink(f"{path}:Zone.Identifier")`.
   - Salva o estado da fila (`_save_queue_state()`).
   - **Prevenção do erro `Failed to load Python DLL (_MEI...\python312.dll)`:** Como executáveis PyInstaller `--onefile` injetam `_MEIPASS2` e alteram `PATH` no bloco de ambiente do processo, `_launch_self_updater_and_exit` sanitiza o ambiente com `get_clean_pyinstaller_env()` e gera um script temporário em `%TEMP%\_aplicar_atualizacao_bolinha.bat` que:
     - Limpa explicitamente `_MEIPASS`, `_MEIPASS2`, `_PYI_*`, `TCL_LIBRARY`, `TK_LIBRARY`, `PYTHONPATH` e `PYTHONHOME`.
     - Aguarda em loop (`for /L %%i in (1,1,15)`) até conseguir sobrescrever `Telegram_Cloud_Drive.exe` (garantindo que o processo anterior e seu bootloader C já encerraram completamente).
     - Inicia o novo executável com ambiente limpo e se autoexclui.
   - O processo antigo encerra de forma limpa com `self.quit()`, `self.destroy()` e `sys.exit(0)`.

---

## 6. Pontos Críticos, Limitações Conhecidas e Recomendações de Arquitetura

Para outra IA ou desenvolvedor que for evoluir este projeto, estes são os pontos mais sensíveis da arquitetura atual:

1. **Arquitetura Monolítica em Arquivo Único (`main.py` com ~6.460 linhas):**
   - **Situação atual:** Toda a lógica de UI, ícones vetoriais, cliente Telethon, motor de upload paralelo, cache de grupo, auto-updater e desinstalador reside em `main.py`.
   - **Recomendação:** Caso o projeto cresça, modularizar em pacote (`app/ui.py`, `app/telegram_engine.py`, `app/updater.py`, `app/persistence.py`, `app/uninstaller.py`) mantendo um `main.py` enxuto como *entrypoint*.
2. **Credenciais `DEFAULT_API_ID` e `DEFAULT_API_HASH` Compartilhadas:**
   - **Situação atual:** O código traz por padrão `DEFAULT_API_ID = 2040` e `DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"` para que o usuário final possa logar com QR Code ou telefone sem precisar criar uma conta de desenvolvedor em `my.telegram.org`.
   - **Recomendação:** Se o aplicativo for distribuído em larga escala para muitos usuários externos, é recomendável registrar um `api_id` / `api_hash` próprio em `my.telegram.org` ou orientar usuários avançados a configurarem o próprio par na guia `⚙ Opções > Informações da Conta`.
3. **Caminhos Locais no Script de Deploy (`publicar_atualizacao_github.py`):**
   - **Situação atual:** `publicar_atualizacao_github.py` referencia `DESKTOP_DIR = Path(r"C:\Users\tiago\OneDrive - Sistema FIEG\Área de Trabalho")` para localizar as pastas `Portatil` e `Instalador` e o repositório `GITHUB_REPO = "3olinha/Telegram-Cloud-Drive"`.
   - **Recomendação:** Ao portar o ambiente de build para outra máquina ou para GitHub Actions, parametrizar `DESKTOP_DIR` via variável de ambiente ou usar exclusivamente `./dist`.
4. **Assinatura Digital (Code Signing) Autoassinada vs. Certificado EV/OV:**
   - **Situação atual:** O build local assina os binários `.exe` com um certificado local (`Cert:\CurrentUser\My -CodeSigningCert`), o que evita avisos na máquina de desenvolvimento, mas em máquinas de terceiros o Windows SmartScreen ainda pode exibir alerta na primeira execução de um binário PyInstaller `--onefile` não assinado por uma Autoridade Certificadora comercial (DigiCert, Sectigo, SignPath).
5. **Escalabilidade do `ttk.Treeview` Acima de 100.000 Linhas:**
   - **Situação atual:** A `v0.14` introduziu carregamento em cache JSON (`group_cloud_cache.json`), dicionário hash $O(1)$ (`remote_by_filename_lower`) e inserção em lotes assíncronos de 400 linhas (`_render_dl_rows_chunked`), resolvendo o gargalo de rede e de travamento de UI para ~60.000 arquivos.
   - **Recomendação:** Para grupos que ultrapassem 100.000–200.000 arquivos, recomenda-se substituir o `group_cloud_cache.json` por um banco **SQLite local com índice FTS5** e implementar **paginação virtual** na `ttk.Treeview` (renderizando no DOM do Tkinter apenas as ~500 linhas visíveis por página ou filtradas pela busca).
