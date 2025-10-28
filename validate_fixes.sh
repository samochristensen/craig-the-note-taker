#!/usr/bin/env bash
# Quick validation script for all critical fixes
# Run after applying improvements to verify everything works

set -euo pipefail

RED=$'\033[31m'
GREEN=$'\033[32m'
YELLOW=$'\033[33m'
NC=$'\033[0m'

echo "═══════════════════════════════════════════════════════════════"
echo "  Discord Notetaker - Post-Fix Validation"
echo "═══════════════════════════════════════════════════════════════"
echo

# Check 1: Transcriber import chain
echo -n "1. Checking transcriber/main.py imports app.py... "
if grep -q "^from app import app" transcriber/main.py 2>/dev/null; then
    echo "${GREEN}✓ PASS${NC}"
else
    echo "${RED}✗ FAIL${NC} - Missing 'from app import app'"
    exit 1
fi

# Check 2: Audio file seek fix
echo -n "2. Checking audio.file.seek(0) in bot.py... "
if grep -q "audio\.file\.seek(0)" bot/bot.py 2>/dev/null; then
    echo "${GREEN}✓ PASS${NC}"
else
    echo "${RED}✗ FAIL${NC} - Missing audio.file.seek(0)"
    exit 1
fi

# Check 3: Prompt filename fix
echo -n "3. Checking recap_prompts.txt (plural) path... "
if grep -q "recap_prompts\.txt" bot/bot.py 2>/dev/null; then
    echo "${GREEN}✓ PASS${NC}"
else
    echo "${RED}✗ FAIL${NC} - Still using recap_prompt.txt (singular)"
    exit 1
fi

# Check 4: Input validation
echo -n "4. Checking session_id validation in transcriber... "
if grep -q "re\.match.*sid" transcriber/app.py 2>/dev/null; then
    echo "${GREEN}✓ PASS${NC}"
else
    echo "${RED}✗ FAIL${NC} - Missing input validation"
    exit 1
fi

# Check 5: HTTPException import
echo -n "5. Checking FastAPI HTTPException import... "
if grep -q "from fastapi import.*HTTPException" transcriber/app.py 2>/dev/null; then
    echo "${GREEN}✓ PASS${NC}"
else
    echo "${RED}✗ FAIL${NC} - Missing HTTPException import"
    exit 1
fi

# Check 6: Docker resource limits
echo -n "6. Checking docker-compose.yml resource limits... "
if grep -q "deploy:" docker-compose.yml 2>/dev/null && \
   grep -q "resources:" docker-compose.yml 2>/dev/null && \
   grep -q "limits:" docker-compose.yml 2>/dev/null; then
    echo "${GREEN}✓ PASS${NC}"
else
    echo "${RED}✗ FAIL${NC} - Missing resource limits"
    exit 1
fi

# Check 7: .env.example exists
echo -n "7. Checking .env.example template exists... "
if [ -f ".env.example" ]; then
    echo "${GREEN}✓ PASS${NC}"
else
    echo "${RED}✗ FAIL${NC} - Missing .env.example"
    exit 1
fi

# Check 8: Structured logging
echo -n "8. Checking structured logging setup... "
if grep -q "logging\.basicConfig" bot/bot.py 2>/dev/null && \
   grep -q "logger = logging\.getLogger" bot/bot.py 2>/dev/null; then
    echo "${GREEN}✓ PASS${NC}"
else
    echo "${RED}✗ FAIL${NC} - Missing structured logging"
    exit 1
fi

echo
echo "═══════════════════════════════════════════════════════════════"
echo "${GREEN}  ALL CHECKS PASSED ✓${NC}"
echo "═══════════════════════════════════════════════════════════════"
echo
echo "Next steps:"
echo "  1. Copy .env.example to .env and configure"
echo "  2. Run: docker compose up --build"
echo "  3. Test with /startnotes and /stopnotes"
echo "  4. Check logs: docker compose logs -f discord-bot"
echo

exit 0
