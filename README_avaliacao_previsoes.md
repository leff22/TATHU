# Avaliação independente das previsões do TATHU

O script `avaliar_previsoes.py` compara previsões GeoJSON com as detecções reais posteriores. Ele é independente do processamento operacional e não altera os TIFFs, GeoJSONs ou parâmetros do forecast.

## Como executar

No ambiente virtual em que o TATHU e GDAL/OGR já estão instalados, a partir da raiz do repositório:

```powershell
python .\avaliar_previsoes.py --input-dir .\saidas_detecao_script
```

Por padrão, avalia somente +20 minutos e grava `avaliacao_previsoes.csv` dentro do diretório de saídas.

Para avaliar os três horizontes:

```powershell
python .\avaliar_previsoes.py --input-dir .\saidas_detecao_script --leads 20 40 60
```

A associação entre previsão e detecção usa a menor distância entre centróides, sem reutilizar o mesmo polígono, limitada por padrão a 75 km. Esse limite pode ser ajustado com `--max-match-distance-km`.

## Arquivos usados

Para uma previsão iniciada em `YYYYMMDDHHMM` e horizonte de +20 min, o avaliador procura:

- `previsao_YYYYMMDDHHMM.geojson`: polígonos previstos;
- `historico_YYYYMMDDHHMM.geojson`: detecções observadas no horário inicial;
- `historico_YYYYMMDDHHMM+20min.geojson`: detecções reais no horário-alvo.

A comparação só é feita quando os arquivos necessários existem. Assim, previsões recentes ficam pendentes até a detecção do horário-alvo ser publicada.

## Métricas no CSV

- `centroid_error_km`: distância entre centróides previstos e observados;
- `iou`: interseção sobre união dos polígonos;
- `area_error_pct`: diferença percentual de área (positivo = previsão maior);
- `predicted_displacement_km` e `observed_displacement_km`: deslocamento desde a posição inicial;
- `along_track_error_km`: erro no eixo da direção prevista (positivo = previsão adiantada);
- `cross_track_error_km`: erro transversal à direção prevista;
- `status`: `matched`, `forecast_without_observation` ou `observation_without_forecast`.

O CSV é reconstruído a cada execução a partir dos GeoJSONs disponíveis. As rodadas consecutivas são amostras operacionais correlacionadas; 10 rodadas permitem um primeiro diagnóstico, mas a calibração deve considerar também outros sistemas e períodos.
