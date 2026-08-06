"""target_dim() is the single source of the schema dimension. A wrong answer
here either fails every insert or (on a populated table) invites a destructive
migration — so its edge cases are the highest-value thing to pin down."""

import pytest


@pytest.mark.parametrize(
    "model,expected",
    [
        ("text-embedding-3-large", 3072),
        ("text-embedding-3-small", 1536),
        ("text-embedding-ada-002", 1536),
        # Provider prefixes are stripped.
        ("azure/text-embedding-3-large", 3072),
        ("openai/text-embedding-3-small", 1536),
        # Case-insensitive: an uppercase env value must not fall through.
        ("TEXT-EMBEDDING-3-LARGE", 3072),
        ("Azure/Text-Embedding-3-Large", 3072),
        # Version suffix still matches the family.
        ("text-embedding-3-large-v2", 3072),
    ],
)
def test_known_models_resolve(reload_embed, model, expected):
    embed = reload_embed(model=model)
    assert embed.target_dim() == expected


def test_longest_family_wins_no_shadow(reload_embed):
    """`-3-large` must not be shadowed by a shorter substring match."""
    embed = reload_embed(model="text-embedding-3-large")
    assert embed.target_dim() == 3072


def test_unknown_model_raises_not_guesses(reload_embed):
    """An unrecognised model must raise, never silently default — a wrong
    guess on a populated DB is a data-loss trigger."""
    embed = reload_embed(model="ollama/some-new-embedder")
    with pytest.raises(embed.UnknownEmbedDimError):
        embed.target_dim()


def test_embed_dim_env_overrides_everything(reload_embed):
    """EMBED_DIM pins the dimension even for an otherwise-unknown model —
    the operator's explicit escape hatch."""
    embed = reload_embed(model="ollama/some-new-embedder", embed_dim="1024")
    assert embed.target_dim() == 1024


def test_embed_dim_env_overrides_known_model(reload_embed):
    embed = reload_embed(model="text-embedding-3-large", embed_dim="256")
    assert embed.target_dim() == 256
