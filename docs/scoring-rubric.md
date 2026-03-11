# Tool Scoring Rubric

Every tool in the stack gets evaluated before committing to it. No tool enters the harness without scoring.

## Scoring Dimensions

| Dimension | Weight | 1 (Bad) | 3 (Okay) | 5 (Great) |
|-----------|--------|---------|----------|-----------|
| **Setup time** | 20% | >2 hours to get running | 30-60 min | <15 min, single command |
| **MCP integration** | 20% | No API, scraping required | REST API, needs adapter | MCP server exists or trivial to wrap |
| **False positive rate** | 15% | >30% noise | 10-30% | <10% actionable signal |
| **Maintenance burden** | 15% | Constant config tweaking | Monthly updates | Set and forget |
| **Multi-project support** | 15% | Hardcoded to one repo | Config per repo | Shared instance, per-project config |
| **Cost** | 10% | >$50/mo | Free tier + paid features | Fully free/OSS |
| **Community/maturity** | 5% | <100 stars, 1 maintainer | 1k+ stars, active issues | 10k+ stars, corporate backing |

## Scoring Formula
```
Score = Σ(dimension_score × weight) / 5
Range: 0.0 (reject) to 1.0 (perfect)
Threshold: 0.6 to include, 0.8 to be primary choice
```

## Current Scores

### Layer 1: Intake
| Tool | Setup | MCP | FP Rate | Maint | Multi-proj | Cost | Community | **Total** |
|------|-------|-----|---------|-------|-----------|------|-----------|-----------|
| beads (br) | 5 | 5 | N/A | 5 | 3 | 5 (OSS) | 4 | **0.92** |
| Beads-Kanban-UI | ? | ? | N/A | ? | ? | 5 (OSS) | ? | **TBD** |

### Layer 2: Orchestration
| Tool | Setup | MCP | FP Rate | Maint | Multi-proj | Cost | Community | **Total** |
|------|-------|-----|---------|-------|-----------|------|-----------|-----------|
| dmux | 5 | 2 | N/A | 4 | 5 | 5 (OSS) | 3 | **0.80** |
| Gastown | ? | ? | N/A | ? | ? | 5 (OSS) | ? | **TBD** |
| JAT | ? | ? | N/A | ? | ? | 5 (OSS) | ? | **TBD** |
| Symphony | ? | ? | N/A | ? | ? | 5 (OSS) | ? | **TBD** |

### Layer 3: Runtime
| Tool | Setup | MCP | FP Rate | Maint | Multi-proj | Cost | Community | **Total** |
|------|-------|-----|---------|-------|-----------|------|-----------|-----------|
| OpenFang | ? | ? | N/A | ? | ? | 5 (OSS) | ? | **TBD** |
| zsh-tool MCP | ? | ? | ? | ? | ? | 5 (OSS) | ? | **TBD** |
| Continuous-Claude-v3 | ? | ? | N/A | ? | ? | 5 (OSS) | ? | **TBD** |
| Letta Context Repos | ? | ? | N/A | ? | ? | 5 (OSS) | ? | **TBD** |
| Headroom | ? | ? | N/A | ? | ? | 5 (OSS) | ? | **TBD** |
| EnCompass | ? | ? | N/A | ? | ? | 5 (OSS) | ? | **TBD** |

### Layer 4: Quality Gates
| Tool | Setup | MCP | FP Rate | Maint | Multi-proj | Cost | Community | **Total** |
|------|-------|-----|---------|-------|-----------|------|-----------|-----------|
| DeepEval (LLM-as-judge) | 4 | 3 | 3 | 3 | 4 | 5 (OSS) | 5 | **0.73** |
| VibeForge Scanner | ? | ? | ? | ? | ? | 5 (OSS) | ? | **TBD** |
| gitleaks | 5 | 3 | 4 | 4 | 5 | 5 (OSS) | 5 | **0.86** |
| ATDD | ? | ? | ? | ? | ? | 5 (OSS) | ? | **TBD** |

### Layer 5: Observability
| Tool | Setup | MCP | FP Rate | Maint | Multi-proj | Cost | Community | **Total** |
|------|-------|-----|---------|-------|-----------|------|-----------|-----------|
| OpenObserve | 4 | 4 | N/A | 3 | 5 | 5 (OSS) | 5 | **0.83** |
| AgentLens | ? | ? | N/A | ? | ? | 5 (OSS) | ? | **TBD** |
| Agent Trace | ? | ? | N/A | ? | ? | 5 (OSS) | ? | **TBD** |

### Layer 6: Feedback Loop
| Tool | Setup | MCP | FP Rate | Maint | Multi-proj | Cost | Community | **Total** |
|------|-------|-----|---------|-------|-----------|------|-----------|-----------|
| DeepEval Step Efficiency | ? | ? | ? | ? | ? | 5 (OSS) | ? | **TBD** |

## Scoring Process

1. **Before TB-1**: Score all TB-1 tools (beads, dmux, DeepEval, gitleaks, OpenObserve)
2. **After TB-1**: Re-score based on real usage data
3. **Before each TB**: Score any new tools that TB introduces
4. **Monthly**: Re-score all tools based on accumulated experience

## Kill Criteria
A tool gets removed if:
- Score drops below 0.5 after real usage
- False positive rate exceeds 30% for 2 consecutive weeks
- Maintenance requires >1 hour/week of manual intervention
- A simpler alternative scores 0.15+ higher

## Replacement Protocol
1. Score the replacement against the rubric
2. Run replacement in shadow mode alongside current tool for 1 week
3. Compare results (quality, speed, cost, noise)
4. If replacement wins: swap in next TB, remove old tool
5. Update ADR documenting the switch
