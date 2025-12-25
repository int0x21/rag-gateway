#!/usr/bin/env bash
# RAG Gateway Validation Script
# Run this after install.sh to validate all components are working

set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
API_URL="http://127.0.0.1:9000"
QDRANT_URL="http://127.0.0.1:6333"
TEI_EMBED_URL="http://127.0.0.1:8081"
TEI_RERANK_URL="http://127.0.0.1:8082"
VLLM_URL="http://127.0.0.1:8000"

# Logging
log() { echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $*" >&2; }
success() { echo -e "${GREEN}✓${NC} $*" >&2; }
warning() { echo -e "${YELLOW}⚠${NC} $*" >&2; }
error() { echo -e "${RED}✗${NC} $*" >&2; }
info() { echo -e "${BLUE}ℹ${NC} $*" >&2; }

# Test functions
test_service_status() {
    local service="$1"
    local expected_status="${2:-active}"

    log "Checking $service status..."
    if systemctl is-active --quiet "$service" 2>/dev/null; then
        success "$service is $expected_status"
        return 0
    else
        error "$service is not $expected_status"
        return 1
    fi
}

test_http_endpoint() {
    local url="$1"
    local description="$2"
    local expected_code="${3:-200}"

    log "Testing $description..."
    if response=$(curl -s -w "HTTPSTATUS:%{http_code}" "$url" 2>/dev/null); then
        local body=$(echo "$response" | sed 's/HTTPSTATUS.*//')
        local code=$(echo "$response" | grep "HTTPSTATUS:" | sed 's/.*HTTPSTATUS://')

        if [ "$code" = "$expected_code" ]; then
            success "$description responded with HTTP $code"
            echo "$body"  # Return response body for further processing
            return 0
        else
            error "$description failed: HTTP $code (expected $expected_code)"
            echo "$body" >&2
            return 1
        fi
    else
        error "$description failed: connection error"
        return 1
    fi
}

test_json_response() {
    local url="$1"
    local description="$2"

    log "Testing $description JSON response..."
    if response=$(curl -s "$url" 2>/dev/null); then
        if echo "$response" | jq . >/dev/null 2>&1; then
            success "$description returned valid JSON"
            echo "$response"
            return 0
        else
            error "$description returned invalid JSON: $response"
            return 1
        fi
    else
        error "$description failed: connection error"
        return 1
    fi
}

test_post_endpoint() {
    local url="$1"
    local description="$2"
    local payload="$3"
    local expected_code="${4:-200}"

    log "Testing $description..."
    if response=$(curl -s -w "HTTPSTATUS:%{http_code}" -X POST "$url" \
        -H "Content-Type: application/json" \
        -d "$payload" 2>/dev/null); then

        local body=$(echo "$response" | sed 's/HTTPSTATUS.*//')
        local code=$(echo "$response" | grep "HTTPSTATUS:" | sed 's/.*HTTPSTATUS://')

        if [ "$code" = "$expected_code" ]; then
            success "$description responded with HTTP $code"
            echo "$body"  # Return response body for further processing
            return 0
        else
            error "$description failed: HTTP $code (expected $expected_code)"
            echo "$body" >&2
            return 1
        fi
    else
        error "$description failed: connection error"
        return 1
    fi
}

test_chat_completions() {
    local url="$1"

    log "Testing chat completions with RAG..."
    local payload='{"model": "generator", "messages": [{"role": "user", "content": "What is RAG?"}], "rag": {"mode": "selection"}}'

    if response=$(curl -s -X POST "$url" \
        -H "Content-Type: application/json" \
        -d "$payload" 2>/dev/null); then

        # Check if it's valid JSON
        if echo "$response" | jq '.choices[0].message.content' >/dev/null 2>&1; then
            local content=$(echo "$response" | jq -r '.choices[0].message.content' | head -c 100)
            success "Chat completions returned valid response: ${content}..."
            return 0
        elif echo "$response" | jq '.detail' >/dev/null 2>&1; then
            local error_msg=$(echo "$response" | jq -r '.detail')
            warning "Chat completions returned error (but valid JSON): $error_msg"
            return 1
        else
            error "Chat completions returned invalid response: $response"
            return 1
        fi
    else
        error "Chat completions failed: connection error"
        return 1
    fi
}

test_file_exists() {
    local path="$1"
    local description="$2"

    log "Checking $description..."
    if [ -e "$path" ]; then
        success "$description exists"
        return 0
    else
        error "$description does not exist: $path"
        return 1
    fi
}

test_directory_not_empty() {
    local path="$1"
    local description="$2"

    log "Checking $description..."
    if [ -d "$path" ] && [ "$(find "$path" -mindepth 1 | wc -l)" -gt 0 ]; then
        success "$description has content"
        return 0
    else
        warning "$description is empty or does not exist"
        return 1
    fi
}

# Main validation
main() {
    local failed_tests=0

    echo
    echo "========================================="
    echo "🧪 RAG Gateway Validation Script"
    echo "========================================="
    echo

    # 1. Service Status Checks
    info "Phase 1: Service Status Checks"
    echo

    test_service_status "rag-gateway.service" || ((failed_tests++))
    test_service_status "qdrant.service" || ((failed_tests++))
    test_service_status "tei-embed.service" || ((failed_tests++))
    test_service_status "tei-rerank.service" || ((failed_tests++))
    test_service_status "vllm.service" || ((failed_tests++))

    echo

    # 2. Component Health Checks
    info "Phase 2: Component Health Checks"
    echo

    # Test Qdrant
    if collections=$(test_http_endpoint "$QDRANT_URL/collections" "Qdrant collections endpoint"); then
        collection_count=$(echo "$collections" | jq '.result.collections | length' 2>/dev/null || echo "0")
        info "Qdrant has $collection_count collections"
    else
        ((failed_tests++))
    fi

    # Test TEI embedding
    embed_payload='{"model": "Qwen/Qwen3-Embedding-8B", "input": ["test query"]}'
    if response=$(test_post_endpoint "$TEI_EMBED_URL/v1/embeddings" "TEI embedding endpoint" "$embed_payload"); then
        if echo "$response" | jq '.data[0].embedding | length' >/dev/null 2>&1; then
            success "TEI embedding returned valid vector data"
        else
            error "TEI embedding returned invalid response: $response"
            ((failed_tests++))
        fi
    else
        ((failed_tests++))
    fi

    # Test TEI reranking
    rerank_payload='{"model": "bge-reranker-large", "query": "test query", "texts": ["test text 1", "test text 2"]}'
    if response=$(test_post_endpoint "$TEI_RERANK_URL/rerank" "TEI reranking endpoint" "$rerank_payload"); then
        if echo "$response" | jq 'length' >/dev/null 2>&1 && [ "$(echo "$response" | jq 'length')" -eq 2 ]; then
            success "TEI reranking returned valid ranking data"
        else
            error "TEI reranking returned invalid response: $response"
            ((failed_tests++))
        fi
    else
        ((failed_tests++))
    fi

    # Test VLLM models
    test_json_response "$VLLM_URL/v1/models" "VLLM models endpoint" || ((failed_tests++))

    echo

    # 3. API Endpoint Tests
    info "Phase 3: API Endpoint Tests"
    echo

    # Test RAG Gateway health
    test_json_response "$API_URL/health" "RAG Gateway health endpoint" || ((failed_tests++))

    # Test models endpoint
    test_json_response "$API_URL/v1/models" "RAG Gateway models endpoint" || ((failed_tests++))

    # Test embeddings endpoint
    local embed_payload='{"input": "test query", "model": "Qwen/Qwen3-Embedding-8B"}'
    if response=$(curl -s -X POST "$API_URL/v1/embeddings" \
        -H "Content-Type: application/json" \
        -d "$embed_payload" 2>/dev/null); then

        if echo "$response" | jq '.data[0].embedding | length' >/dev/null 2>&1; then
            local embed_length=$(echo "$response" | jq '.data[0].embedding | length')
            success "Embeddings endpoint returned $embed_length-dimensional vector"
        else
            error "Embeddings endpoint returned invalid response: $response"
            ((failed_tests++))
        fi
    else
        error "Embeddings endpoint failed: connection error"
        ((failed_tests++))
    fi

    # Test chat completions
    test_chat_completions "$API_URL/v1/chat/completions" || ((failed_tests++))

    echo

    # 4. Storage & File Checks
    info "Phase 4: Storage & File Checks"
    echo

    test_file_exists "/opt/llm/rag-gateway/var/tantivy" "Tantivy index directory" || ((failed_tests++))
    test_file_exists "/opt/llm/rag-gateway/var/log" "Log directory" || ((failed_tests++))
    test_file_exists "/etc/rag-gateway/api.yaml" "API config file" || ((failed_tests++))
    test_file_exists "/etc/rag-gateway/ingest.yaml" "Ingest config file" || ((failed_tests++))
    test_file_exists "/etc/rag-gateway/sources.yaml" "Sources config file" || ((failed_tests++))

    # Check if directories have content
    test_directory_not_empty "/opt/llm/rag-gateway/var/tantivy" "Tantivy index" || true  # Not a failure if empty
    test_directory_not_empty "/opt/llm/rag-gateway/var/log" "Log directory" || true  # Not a failure if empty

    echo

    # 5. CLI Tool Check
    info "Phase 5: CLI Tool Check"
    echo

    if command -v rag-gateway-crawl >/dev/null 2>&1; then
        success "rag-gateway-crawl CLI tool is installed"

        # Test CLI help
        if rag-gateway-crawl --help >/dev/null 2>&1; then
            success "CLI tool help works"
        else
            error "CLI tool help failed"
            ((failed_tests++))
        fi
    else
        error "rag-gateway-crawl CLI tool is not installed"
        ((failed_tests++))
    fi

    echo

    # Summary
    echo "========================================="
    if [ $failed_tests -eq 0 ]; then
        echo -e "${GREEN}🎉 All validation tests passed!${NC}"
        echo "Your RAG Gateway is fully operational."
    else
        echo -e "${RED}❌ $failed_tests validation test(s) failed.${NC}"
        echo "Please check the errors above and fix the issues."
        echo
        echo "Common fixes:"
        echo "  - Restart services: sudo systemctl restart rag-gateway.service"
        echo "  - Check logs: journalctl -u rag-gateway.service -n 50"
        echo "  - Verify configs: cat /etc/rag-gateway/api.yaml"
        echo "  - Test components individually"
    fi
    echo "========================================="

    return $failed_tests
}

# Run main function
main "$@"