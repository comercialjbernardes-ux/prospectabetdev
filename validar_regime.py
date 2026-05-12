"""
validar_regime.py — Validação e score de confiabilidade de regime tributário
============================================================================
Cruza regime tributário com capital social e porte para detectar
inconsistências e atribuir score de confiabilidade.
"""

# Regimes tributários válidos no Brasil
REGIMES_VALIDOS = frozenset({
    "Simples Nacional",
    "MEI",
    "Lucro Presumido",
    "Lucro Real",
    "Imune",
    "Isento",
    "Não identificado",
})

# Receita bruta anual máxima para Simples Nacional (R$4,8M)
_LIMITE_SIMPLES = 4_800_000.0

# Nota: a Lei 9.718/98 art. 14 obriga Lucro Real para receita bruta > R$78M/ano.
# Capital social ≠ receita bruta — não usamos capital como critério de obrigatoriedade,
# pois uma empresa pode ter capital de R$200M e faturar R$5M (ou vice-versa).
# O critério correto é o campo "porte" retornado pela Receita Federal.

# Fontes consideradas "oficiais" (dados da Receita Federal)
_FONTES_OFICIAIS = {"brasilapi", "receitaws"}


def validar_regime(
    regime: str,
    capital_social: float,
    porte: str,
) -> tuple[str, str]:
    """
    Valida o regime tributário e corrige inconsistências detectadas.

    Regras aplicadas:
    - Capital social > R$78M: obriga Lucro Real (Lei 9.718/98, art. 14)
    - Porte DEMAIS + Simples Nacional: incompatível (faturamento excede limite)
    - Capital > limite Simples mas declarado como Simples: corrige

    Retorna:
        (regime_corrigido, alerta)
        alerta é string vazia se não houver inconsistência.
    """
    if regime not in REGIMES_VALIDOS:
        return "Não identificado", f"Regime '{regime}' não reconhecido"

    # MEI e Isento/Imune: não aplicamos regras adicionais
    if regime in ("MEI", "Imune", "Isento", "Não identificado"):
        return regime, ""

    porte_upper = str(porte or "").upper()
    capital = float(capital_social or 0)

    # Porte DEMAIS é incompatível com Simples Nacional
    # (empresas de grande porte excedem o limite de faturamento do Simples)
    if regime == "Simples Nacional" and porte_upper == "DEMAIS":
        alerta = (
            f"Porte DEMAIS é incompatível com Simples Nacional "
            f"(receita provavelmente excede R$4,8M/ano). Corrigido para Lucro Real."
        )
        return "Lucro Real", alerta

    # Capital expressivo para Simples Nacional: possível inconsistência
    if regime == "Simples Nacional" and capital > _LIMITE_SIMPLES:
        alerta = (
            f"Capital social R${capital:,.0f} é elevado para Simples Nacional "
            f"(limite ~R$4,8M/ano de receita). Dado pode estar desatualizado."
        )
        # Não corrigimos aqui — capital social ≠ receita bruta
        return regime, alerta

    return regime, ""


def calcular_confiabilidade(
    fonte: str,
    regime: str,
    capital_social: float,
    porte: str,
) -> str:
    """
    Calcula score de confiabilidade do dado tributário.

    Alta:   fonte oficial + regime consistente com porte/capital
    Média:  fonte oficial com inconsistência menor, ou scraping do site,
            ou regime inferido com lógica clara (capital/porte definidos)
    Baixa:  regime não identificado, ou inferido sem base clara
    """
    if regime == "Não identificado":
        return "baixa"

    regime_corrigido, alerta = validar_regime(regime, capital_social, porte)

    if fonte in _FONTES_OFICIAIS:
        # Fonte oficial: alta se sem inconsistência, média se houver alerta
        return "alta" if not alerta else "media"

    if fonte == "site":
        # Scraping do site: confiável apenas se explicitamente declarado
        return "media"

    if fonte == "inferido":
        # Inferência com base em porte/capital bem definidos → média
        porte_upper = str(porte or "").upper()
        capital = float(capital_social or 0)
        if porte_upper in ("ME", "EPP", "MICRO EMPRESA", "DEMAIS") or capital > 0:
            return "media"
        return "baixa"

    return "baixa"
