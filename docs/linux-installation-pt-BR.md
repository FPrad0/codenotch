# Codenotch no Linux (experimental)

O Codenotch ainda não publica um instalador Linux nas releases. Na revisão de 22/10/2026, a release `v1.23.0` (commit `9e7cd3d`) oferecia o app macOS (`.dmg`) e o instalador Windows (`.exe`), sem pacote Linux. O código Linux disponível fica no port Rust/Tauri de `windows/`, e pode ser compilado a partir do código-fonte.

O CI do projeto compila e executa os testes Rust no Ubuntu, mas não testa a janela em um desktop gráfico. Considere o suporte Linux experimental.

## Pré-requisitos no Ubuntu ou Debian

Instale as bibliotecas necessárias para compilar o WebView e a bandeja:

```sh
sudo apt update
sudo apt install build-essential pkg-config libssl-dev libwebkit2gtk-4.1-dev \
                 libgtk-3-dev libayatana-appindicator3-dev librsvg2-dev \
                 libxdo-dev curl wget file
```

Essas dependências seguem o port Linux do projeto e os [pré-requisitos oficiais do Tauri 2](https://v2.tauri.app/start/prerequisites/).

Instale também o Rust estável pelo [rustup](https://rustup.rs/):

```sh
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
. "$HOME/.cargo/env"
rustup default stable
```

## Compilar e iniciar

Clone o repositório e compile o port Rust/Tauri:

```sh
git clone https://github.com/vinzdg/codenotch.git
cd codenotch/windows
cargo build --release --locked -p codenotch
```

Inicie o app pelo script do projeto:

```sh
./scripts/run-linux.sh
```

Para executar o diagnóstico local:

```sh
./scripts/run-linux.sh doctor
```

O script inicia a janela como cliente X11. O Wayland não permite que o app posicione a própria janela na borda da tela, então em sessões Wayland o Codenotch depende do XWayland. O script também remove variáveis de ambiente de bibliotecas que podem ser exportadas por aplicativos Snap, evitando conflito com a glibc do sistema.

## Limitações atuais

- O arraste da pílula pela borda ainda não funciona no Linux.
- A detecção de janela em primeiro plano e o retorno automático ao terminal ainda dependem de APIs Windows.
- A leitura de credenciais do Antigravity depende do Credential Manager do Windows.
- Ícones de aplicativos instalados extraídos de arquivos `.exe` não estão disponíveis; os glifos embutidos continuam sendo usados.
- No GNOME, a bandeja exige a extensão **AppIndicator Support**. A disponibilidade da bandeja também varia entre ambientes de desktop.

Os dados locais seguem os diretórios XDG (por padrão, `~/.config/codenotch`). O início automático usa uma entrada XDG em `~/.config/autostart/codenotch.desktop`. Alguns exemplos de fontes de provedores no Linux são `~/.claude`, `~/.codex`, `~/.grok` e `~/.config/Cursor/User/globalStorage/state.vscdb`; cada provedor depende de sua ferramenta e sessão já estarem configuradas.

## Pacotes Linux

A configuração Tauri em `windows/codenotch/tauri.linux.conf.json` declara os formatos `.deb` e AppImage. Porém, a release atual não publica esses pacotes, e este fluxo de empacotamento ainda não é documentado como uma instalação oficial. Use a compilação e execução pelo script acima até que o projeto publique artefatos Linux testados.
