# Perguntas do Projeto

> Este documento define o que o projeto precisa responder.
> Cada pergunta indica os **dados necessários** e **onde ela aparece**. Isso orienta
> a modelagem do banco (Etapa 1) e o pipeline (Etapas 2 e 3).

## Objetivo do projeto

Aplicação web com análise de dados dos meus treinos de corrida (Garmin → Strava), com duas partes:

1. **Dashboard de Performance:** histórico e métricas das últimas corridas.
2. **Simulador de Prova:** estimativa de pace e tempo total para uma prova futura.

## Escopo do MVP

> Importar minhas corridas, mostrar 4 métricas no dashboard e estimar o tempo de uma prova.

**Critério de sucesso do MVP:**
- [ ] Todas as minhas corridas do Strava estão no banco, sem duplicatas
- [ ] O dashboard mostra as 4 métricas marcadas como MVP
- [ ] O simulador devolve um tempo estimado para uma distância informada

---

## 1. Dashboard de Performance

| ID | Pergunta | Dados necessários | Aba do dashboard | Fase |
|----|----------|-------------------|------------------|------|
| D1 | Quantos km corri por semana nos últimos 3 meses? | data, distância | Volume | MVP |
| D2 | Meu pace médio está melhorando ao longo dos meses? | data, distância, tempo em movimento | Ritmo | MVP |
| D3 | Qual minha FC média nos treinos leves e nos treinos fortes? | FC média, tipo de treino | Ritmo e FC | MVP |
| D4 | Quanta altimetria (ganho de elevação) acumulei por semana? | data, ganho de elevação | Volume | MVP |
| D5 | Quais são minhas melhores marcas (5 km, 10 km, maior longão)? | distância, tempo, splits | Recordes | v2 |
| D6 | Quantas corridas fiz por mês e qual a distância média delas? | data, distância | Volume | v2 |
| D7 | Como minha FC se comporta ao longo de um longão (sobe com o mesmo pace)? | série de FC e pace por km | Ritmo e FC | v3 |
| D8 | Em quais percursos/regiões eu mais corro, e como é o perfil de altimetria deles? | coordenadas, altitude | Mapa | v3 |

## 2. Simulador de Prova

| ID | Pergunta | Dados necessários | Fase |
|----|----------|-------------------|------|
| S1 | Qual o tempo estimado para uma prova de X km, com base nas minhas últimas corridas? | histórico de corridas (distância, tempo) | MVP |
| S2 | Quanto as subidas custam no meu ritmo? | pace por trecho, gradiente (% de inclinação) | v2 |
| S3 | Meu pace cai quando faz calor? Quanto por °C? | pace, temperatura e umidade de cada treino | v2 |
| S4 | Qual o tempo estimado para a prova X, no percurso Y, com a previsão de clima Z? | histórico, GPX da prova, clima | v2 |
| S5 | Quais seriam meus parciais por km ao longo da prova? | percurso km a km, modelo de pace | v3 |
| S6 | Qual a faixa de tempo provável (melhor e pior cenário)? | previsões com intervalo | v3 |
| S7 | O quanto minhas estimativas erraram nas corridas passadas? (backtesting) | previsto vs. real | v3 |

## 3. Qualidade e Engenharia de Dados

| ID | Pergunta | Como verificar | Fase |
|----|----------|----------------|------|
| Q1 | Todas as corridas do Strava foram importadas? | contagem no Strava vs. contagem no banco | MVP |
| Q2 | Rodar o pipeline duas vezes duplica dados? | checar `COUNT(*)` e IDs repetidos | MVP |
| Q3 | Existem corridas com dados inválidos (distância zero, FC fora de 30–230, pace impossível)? | consultas de validação em SQL | v2 |
| Q4 | Quando foi a última atualização dos dados? | coluna `ingested_at` | v2 |
| Q5 | O pipeline avisa quando falha? | logs e alertas | v3 |

---

## Definições (para evitar ambiguidade)

- **Pace:** tempo para percorrer 1 km, em min/km (ex.: 5:30 min/km).
- **Tempo em movimento:** tempo total menos as paradas (o Strava informa os dois).
- **Treino leve:** corrida com FC média de até 75% da minha FC máxima.
- **Treino forte:** corrida com FC média acima de 85% da minha FC máxima.
- **Treino moderado:** o que ficar entre os dois critérios acima.
- **Longão:** corrida com 12km ou mais.
- **Semana:** segunda a domingo (padrão ISO).

## Decisões tomadas (podem ser revistas)

- Histórico considerado no simulador: últimos 90 dias de treino (ajustar após o backtest).
- Só atividades do tipo corrida (`Run`) entram no projeto.
- FC máxima de referência: 205bpm
- Primeira prova-alvo para testar o simulador: (Açaí Sport, 5km, 06/12/2026)