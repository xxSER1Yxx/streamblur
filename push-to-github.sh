#!/usr/bin/env bash
# Загрузка StreamBlur на GitHub одним скриптом.
#
#   bash push-to-github.sh
#
# Скрипт спросит логин и токен. Токен не сохраняется ни в файлах, ни в истории
# git — он живёт только в памяти процесса, поэтому git придётся спросить его
# ещё раз при следующем push (см. docs/UPLOAD.md, как запомнить навсегда).

set -euo pipefail

REPO_HOST="github.com"
REPO_OWNER="xxSER1Yxx"
REPO_NAME="streamblur"
REMOTE_URL="https://${REPO_HOST}/${REPO_OWNER}/${REPO_NAME}.git"

# Цвета, если терминал их поддерживает
if [ -t 1 ]; then
  B=$'\033[1m'; G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; N=$'\033[0m'
else
  B=""; G=""; R=""; Y=""; N=""
fi
say()  { printf '%s\n' "$*"; }
ok()   { printf '%s%s%s\n' "$G" "✓ $*" "$N"; }
warn() { printf '%s%s%s\n' "$Y" "! $*" "$N"; }
die()  { printf '%s%s%s\n' "$R" "✗ $*" "$N" >&2; exit 1; }

cd "$(dirname "$0")"

# git должен быть установлен
command -v git >/dev/null 2>&1 || die "git не найден. Установите git и запустите скрипт заново."

say "${B}StreamBlur → github.com/${REPO_OWNER}/${REPO_NAME}${N}"
say ""

# ------------------------------------------------------- 1. тяжёлые файлы
# GitHub режет файлы больше 100 МБ и ругается на всё, что больше 50 МБ.
# Веса нейросетей в репозиторий попадать не должны — они скачиваются командой
# `streamblur safety-models --download`.
say "${B}[1/6] Проверяю, нет ли тяжёлых файлов…${N}"
heavy=0
while IFS= read -r file; do
  [ -z "$file" ] && continue
  size=$(du -m -- "$file" 2>/dev/null | cut -f1)
  [ -z "$size" ] && continue
  if [ "$size" -ge 50 ]; then
    warn "тяжёлый файл ($size МБ): $file"
    heavy=$((heavy + 1))
  fi
done < <(git ls-files --cached --others --exclude-standard 2>/dev/null || find . -type f -not -path "./.git/*")

if [ "$heavy" -gt 0 ]; then
  die "найдено тяжёлых файлов: $heavy. Добавьте их в .gitignore и запустите заново."
fi
ok "тяжёлых файлов нет"

# ------------------------------------------------------- 2. git init
say "${B}[2/6] Инициализирую репозиторий…${N}"
if [ -d .git ]; then
  ok "уже инициализирован (ветка: $(git branch --show-current 2>/dev/null || echo '?'))"
else
  git init -q -b main
  ok "создан, ветка main"
fi

# ------------------------------------------------------- 3. коммит
say "${B}[3/6] Собираю файлы в коммит…${N}"

# git отказывается делать коммит без имени автора. На чистой машине их нет,
# поэтому подставляем свои, если не заданы (только для этого репозитория).
if [ -z "$(git config user.name 2>/dev/null)" ]; then
  git config user.name "$REPO_OWNER"
  warn "имя автора git не было задано — подставил «$REPO_OWNER»"
fi
if [ -z "$(git config user.email 2>/dev/null)" ]; then
  git config user.email "${REPO_OWNER}@users.noreply.github.com"
  warn "e-mail автора git не был задан — подставил служебный адрес GitHub"
fi

git add -A
count=$(git diff --cached --name-only | wc -l | tr -d ' ')
if [ "$count" -eq 0 ] && git rev-parse HEAD >/dev/null 2>&1; then
  ok "изменений нет, отправлю то, что уже закоммичено"
else
  git commit -q -m "StreamBlur: блюр экрана для стримеров по таймингам + нейросеть-страховка"
  ok "в коммите файлов: $count"
fi
say "    размер: $(git ls-files --cached | xargs du -k 2>/dev/null | awk '{s+=$1} END {print s}') КБ"

# ------------------------------------------------------- 4. remote
say "${B}[4/6] Настраиваю адрес репозитория…${N}"
if git remote get-url origin >/dev/null 2>&1; then
  current=$(git remote get-url origin)
  if [ "$current" = "$REMOTE_URL" ]; then
    ok "origin уже указывает куда нужно"
  else
    git remote set-url origin "$REMOTE_URL"
    ok "origin обновлён (было: $current)"
  fi
else
  git remote add origin "$REMOTE_URL"
  ok "origin добавлен"
fi

# ------------------------------------------------------- 5. доступ
say "${B}[5/6] Проверяю доступ к GitHub…${N}"
say "    Логин: ${REPO_OWNER}"
say "    Пароль: ${B}вставьте токен${N} — обычный пароль от аккаунта не подойдёт."
say "    Где взять токен: https://github.com/settings/tokens"
say "    → Generate new token (classic) → отметьте ${B}repo${N} и ${B}workflow${N},"
say "      иначе GitHub отклонит файл автосборки .github/workflows/build.yml"
say "    → скопируйте строку ghp_… (показывается один раз)"
say ""
printf "    Токен (при вводе не отображается): "

if command -v git-credential-manager >/dev/null 2>&1; then
  read -r TOKEN
elif [ -t 0 ]; then
  read -rs TOKEN
  printf '\n'
else
  die "скрипт запущен без терминала — введите токен вручную (см. docs/UPLOAD.md)"
fi

[ -n "$TOKEN" ] || die "токен не введён"
AUTH_URL="https://${REPO_OWNER}:${TOKEN}@${REPO_HOST}/${REPO_OWNER}/${REPO_NAME}.git"

# ------------------------------------------------------- 6. отправка
say "${B}[6/6] Отправляю на GitHub…${N}"

# Вывод git сначала читаем в переменную, а потом подменяем в нём токен на ***.
# Так токен не попадёт ни на экран, ни в лог — независимо от того, как он
# выглядит (ghp_… для classic, github_pat_… для fine-grained).
set +e
output=$(git push -u "$AUTH_URL" main 2>&1)
status=$?
set -e
printf '%s\n' "${output//$TOKEN/***}"

if [ "$status" -eq 0 ]; then
  say ""
  ok "готово: https://github.com/${REPO_OWNER}/${REPO_NAME}"
  say ""
  say "Что дальше:"
  say "  • во вкладке ${B}Actions${N} уже собирается .exe (5–10 минут)"
  say "  • после сборки файлы появятся во вкладке ${B}Releases${N}"
  say "  • лицензия AGPL-3.0 определится автоматически"
else
  say ""
  warn "не отправилось (код $status). Частые причины:"
  if printf '%s' "$output" | grep -q "without .workflow. scope"; then
    say "  ${B}• у токена нет права workflow${N} — без него GitHub не принимает"
    say "    файл автосборки .github/workflows/build.yml."
    say "    Пересоздайте токен с галочками ${B}repo${N} + ${B}workflow${N} и запустите заново."
  else
    say "  • токен неверный или без права repo → создайте новый"
    say "  • репозиторий не создан или называется не «${REPO_NAME}»"
    say "  • на GitHub уже есть README → git pull --rebase origin main, затем заново"
  fi
  exit 1
fi
