// Package config ports PR-AF's config.py plus the app.py:62-77
// _resolve_budget_caps cascade. Every env var is read at CALL time (inside the
// FromEnv / default constructors), never at package init, so a t.Setenv in a
// test is deterministic and no value is frozen at import.
package config

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// AIIntegrationConfig ports config.py AIIntegrationConfig. Env precedence and
// defaults per design §C.2. The HarnessModel/AIModel fallback is the CODE
// default "minimax/minimax-m2.5" — deliberately different from the
// Docker/compose/manifest default (deepseek/deepseek-v4-flash-0731); env always
// wins, and both facts ship intentionally (design §B.6). Do not "fix" it.
type AIIntegrationConfig struct {
	Provider              string  `json:"provider"`
	HarnessModel          string  `json:"harness_model"`
	AIModel               string  `json:"ai_model"`
	MaxTurns              int     `json:"max_turns"`
	MaxRetries            int     `json:"max_retries"`
	InitialBackoffSeconds float64 `json:"initial_backoff_seconds"`
	MaxBackoffSeconds     float64 `json:"max_backoff_seconds"`
	OpencodeBin           string  `json:"opencode_bin"`
	HarnessBin            string  `json:"harness_bin"`
	OpencodeServer        *string `json:"opencode_server"`
}

// AIConfigFromEnv resolves the AI integration config from the environment
// (config.py AIIntegrationConfig.from_env / its default_factory lambdas).
// A malformed numeric env value is an error, matching Python where the
// default_factory's int()/float() raises at model construction (which happens
// at module import in app.py — i.e. the node fails to boot).
func AIConfigFromEnv() (AIIntegrationConfig, error) {
	maxTurns, err := intEnv("PR_AF_MAX_TURNS", 50)
	if err != nil {
		return AIIntegrationConfig{}, err
	}
	maxRetries, err := intEnv("PR_AF_AI_MAX_RETRIES", 3)
	if err != nil {
		return AIIntegrationConfig{}, err
	}
	initialBackoff, err := floatEnv("PR_AF_AI_INITIAL_BACKOFF_SECONDS", 2.0)
	if err != nil {
		return AIIntegrationConfig{}, err
	}
	maxBackoff, err := floatEnv("PR_AF_AI_MAX_BACKOFF_SECONDS", 8.0)
	if err != nil {
		return AIIntegrationConfig{}, err
	}
	return AIIntegrationConfig{
		Provider:     strEnv("PR_AF_PROVIDER", "aforge"),
		HarnessModel: strEnv("PR_AF_MODEL", "minimax/minimax-m2.5"),
		// AI_MODEL falls back to PR_AF_MODEL, then to the code default.
		AIModel:               strEnv("PR_AF_AI_MODEL", strEnv("PR_AF_MODEL", "minimax/minimax-m2.5")),
		MaxTurns:              maxTurns,
		MaxRetries:            maxRetries,
		InitialBackoffSeconds: initialBackoff,
		MaxBackoffSeconds:     maxBackoff,
		OpencodeBin:           strEnv("PR_AF_OPENCODE_BIN", "opencode"),
		// An empty generic binary override deliberately means no override.
		HarnessBin:     strEnv("PR_AF_HARNESS_BIN", ""),
		OpencodeServer: lookupPtr("PR_AF_OPENCODE_SERVER"),
	}, nil
}

// ProviderEnv builds the subprocess environment forwarded to the opencode
// harness (config.py provider_env): the LLM/GitHub credentials that are set,
// plus an XDG_DATA_HOME that is created if missing.
func (c AIIntegrationConfig) ProviderEnv() map[string]string {
	env := map[string]string{}
	for _, key := range []string{
		"OPENROUTER_API_KEY",
		"ANTHROPIC_API_KEY",
		"OPENAI_API_KEY",
		"GOOGLE_API_KEY",
		"GH_TOKEN",
	} {
		if v := Credential(key); v != "" {
			env[key] = v
		}
	}
	xdg := os.Getenv("XDG_DATA_HOME")
	if xdg == "" {
		xdg = filepath.Join(os.TempDir(), "opencode-shared-data")
	}
	_ = os.MkdirAll(xdg, 0o755)
	env["XDG_DATA_HOME"] = xdg
	env["AGENTFIELD_AFORGE_COMMAND"] = strEnv("AGENTFIELD_AFORGE_COMMAND", "exec")
	return env
}

// openRouterRoutingPrefix qualifies a model slug with the provider that
// serves it. opencode resolves a model as "<provider>/<key>".
const openRouterRoutingPrefix = "openrouter/"

// openRouterHarnessProviders are the harness providers whose model string is
// expected to name OpenRouter. Only these are normalised: prefixing a
// codex/gemini/grok model would break it.
var openRouterHarnessProviders = map[string]bool{"opencode": true, "aforge": true}

// HarnessModelForCLI qualifies the harness model with the OpenRouter provider
// (config.openrouter_harness_model in the Python node).
//
// The SDK passes HarnessConfig.Model to `opencode run` as -m verbatim, and
// opencode resolves -m as "<provider>/<key>" against the providers its config
// declares. docker-entrypoint.sh declares exactly one, openrouter, since that
// is the only credential the image is given. A bare slug such as
// "deepseek/deepseek-v4-flash-0731" therefore names the undeclared provider
// "deepseek": opencode exits 1 within seconds having written no output file,
// and the SDK reports only that the process produced no output.
//
// Normalising the generated config file is not sufficient, because -m
// overrides its model field. The SDK's own opencode tests expect this
// qualified form (-m openrouter/z-ai/glm-5.2).
//
// Safe for aforge, whose provider strips one leading openrouter/ before
// invoking the CLI. Any other provider is returned untouched.
func HarnessModelForCLI(model, provider string) string {
	model = strings.TrimSpace(model)
	if !openRouterHarnessProviders[strings.ToLower(strings.TrimSpace(provider))] {
		return model
	}
	if model == "" || strings.HasPrefix(model, openRouterRoutingPrefix) {
		return model
	}
	return openRouterRoutingPrefix + model
}

// --- shared env readers (call-time only) ---

// Credential returns the environment value for key with surrounding whitespace
// trimmed (config.credential in the Python node).
//
// Secrets pass through several hands before reaching a provider — a GitHub
// Actions secret, a compose environment: entry, a .env file — and each stores
// the value byte for byte. A key pasted with a trailing newline is forwarded
// verbatim, so the provider receives an "Authorization: Bearer <key><newline>"
// header it cannot match. OpenRouter answers that with
//
//	401 {"error": {"message": "User not found.", "code": 401}}
//
// which is indistinguishable from a genuinely unknown key, sending the operator
// off to re-validate a key that is in fact fine. Trimming is safe: no provider
// issues a key whose value depends on surrounding whitespace. Read credentials
// through this rather than os.Getenv so the trim cannot be missed at a seam.
func Credential(key string) string {
	return strings.TrimSpace(os.Getenv(key))
}

// strEnv returns the env value for key, or def when the key is unset. A key that
// is set (even to "") returns its value, matching Python's os.getenv(key, def).
func strEnv(key, def string) string {
	if v, ok := os.LookupEnv(key); ok {
		return v
	}
	return def
}

// intEnv parses key as an int, falling back to def when unset. A set-but-
// malformed value is an error with Python's int() message shape — Python's
// int(os.getenv(...)) raises, it never silently defaults.
func intEnv(key string, def int) (int, error) {
	v, ok := os.LookupEnv(key)
	if !ok {
		return def, nil
	}
	n, err := strconv.Atoi(strings.TrimSpace(v))
	if err != nil {
		return 0, fmt.Errorf("invalid literal for int() with base 10: '%s'", v)
	}
	return n, nil
}

// floatEnv parses key as a float64, falling back to def when unset. A set-but-
// malformed value is an error with Python's float() message shape — Python's
// float(os.getenv(...)) raises, it never silently defaults.
func floatEnv(key string, def float64) (float64, error) {
	v, ok := os.LookupEnv(key)
	if !ok {
		return def, nil
	}
	f, err := strconv.ParseFloat(strings.TrimSpace(v), 64)
	if err != nil {
		return 0, fmt.Errorf("could not convert string to float: '%s'", v)
	}
	return f, nil
}

// lookupPtr returns a pointer to the env value when the key is present (even if
// ""), or nil when unset — the Go analog of os.getenv returning None.
func lookupPtr(key string) *string {
	if v, ok := os.LookupEnv(key); ok {
		return &v
	}
	return nil
}
