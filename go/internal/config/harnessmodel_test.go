package config

import "testing"

// The harness CLI must be invoked with an OpenRouter-qualified model.
//
// The SDK hands HarnessConfig.Model to `opencode run` as -m verbatim, and
// opencode resolves -m as "<provider>/<key>" against the providers its config
// declares. docker-entrypoint.sh declares exactly one, openrouter, as that is
// the only credential the image holds. So the repo default
// "deepseek/deepseek-v4-flash-0731" named the undeclared provider "deepseek":
// opencode exited 1 within seconds having written no output file, and the SDK
// surfaced only that the process produced no output.
//
// Normalising the generated config file was not enough — -m overrides its
// model field — so these tests assert the value handed to the SDK.

const (
	bareSlug  = "deepseek/deepseek-v4-flash-0731"
	qualified = "openrouter/" + bareSlug
)

func TestHarnessModelForCLIQualifiesBareSlug(t *testing.T) {
	for _, provider := range []string{"opencode", "aforge"} {
		t.Run(provider, func(t *testing.T) {
			if got := HarnessModelForCLI(bareSlug, provider); got != qualified {
				t.Errorf("HarnessModelForCLI(%q, %q) = %q, want %q",
					bareSlug, provider, got, qualified)
			}
		})
	}
}

func TestHarnessModelForCLILeavesQualifiedAlone(t *testing.T) {
	if got := HarnessModelForCLI(qualified, "opencode"); got != qualified {
		t.Errorf("got %q, want %q", got, qualified)
	}
}

// PR_AF_PROVIDER is operator input; casing must not silently skip the fix.
func TestHarnessModelForCLIProviderMatchIsLenient(t *testing.T) {
	for _, provider := range []string{"OpenCode", "AFORGE", " opencode "} {
		t.Run(provider, func(t *testing.T) {
			if got := HarnessModelForCLI(bareSlug, provider); got != qualified {
				t.Errorf("provider %q: got %q, want %q", provider, got, qualified)
			}
		})
	}
}

// Prefixing a non-OpenRouter harness would break a working setup.
func TestHarnessModelForCLILeavesOtherProvidersUntouched(t *testing.T) {
	for _, provider := range []string{"codex", "gemini", "grok", "claude"} {
		t.Run(provider, func(t *testing.T) {
			if got := HarnessModelForCLI("gpt-5", provider); got != "gpt-5" {
				t.Errorf("got %q, want %q", got, "gpt-5")
			}
			if got := HarnessModelForCLI(bareSlug, provider); got != bareSlug {
				t.Errorf("got %q, want %q", got, bareSlug)
			}
		})
	}
}

func TestHarnessModelForCLIDoesNotDoublePrefix(t *testing.T) {
	once := HarnessModelForCLI(bareSlug, "opencode")
	if twice := HarnessModelForCLI(once, "opencode"); twice != once {
		t.Errorf("second pass changed %q to %q", once, twice)
	}
}

// An unset model must not turn into the meaningless "openrouter/".
func TestHarnessModelForCLIEmptyStaysEmpty(t *testing.T) {
	for _, model := range []string{"", "   "} {
		if got := HarnessModelForCLI(model, "opencode"); got != "" {
			t.Errorf("HarnessModelForCLI(%q, opencode) = %q, want empty", model, got)
		}
	}
}

func TestHarnessModelForCLITrimsWhitespace(t *testing.T) {
	if got := HarnessModelForCLI("  "+bareSlug+"  ", "opencode"); got != qualified {
		t.Errorf("got %q, want %q", got, qualified)
	}
}

// The repo default, through the real config object, must come out qualified.
func TestDefaultConfigProducesAResolvableHarnessModel(t *testing.T) {
	t.Setenv("PR_AF_MODEL", bareSlug)
	t.Setenv("PR_AF_PROVIDER", "opencode")

	c, err := AIConfigFromEnv()
	if err != nil {
		t.Fatalf("AIConfigFromEnv: %v", err)
	}

	if got := HarnessModelForCLI(c.HarnessModel, c.Provider); got != qualified {
		t.Errorf("got %q, want %q", got, qualified)
	}
}
