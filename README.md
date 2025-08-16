# OfflineWikiSearch
A fast Offline Wikipedia Search Tool for OpenWebUI powered by OpenSearch.

This is meant for LLM's operating in native mode.


Setup - Instructions (for Ubuntu24.04):

0) Download the wiki dump.  
Download the json from https://dumps.wikimedia.org/other/cirrussearch/  
/Current/ may not have the file you need.  
example for english:  

mkdir opensearch  
cd opensearch  
wget https://dumps.wikimedia.org/other/cirrussearch/20250630/enwiki-20250630-cirrussearch-content.json.gz  
gunzip enwiki-20250630-cirrussearch-content.json.gz  

1) Install opensearch:  
curl -fsSL https://artifacts.opensearch.org/publickeys/opensearch.pgp | sudo gpg --dearmor -o /usr/share/keyrings/opensearch-keyring.gpg  
echo "deb [signed-by=/usr/share/keyrings/opensearch-keyring.gpg] https://artifacts.opensearch.org/releases/bundle/opensearch/2.x/apt stable main" | sudo tee /etc/apt/sources.list.d/opensearch-2.x.list  
echo 'DISABLE_INSTALL_DEMO_CONFIG=true' | sudo tee -a /etc/default/opensearch  

sudo tee -a /etc/opensearch/opensearch.yml >/dev/null <<'EOF'  
discovery.type: single-node  
plugins.security.disabled: true  
EOF  

sudo apt update  
sudo apt install opensearch  
sudo systemctl enable --now opensearch  

2) test opensearch:  
curl -s http://localhost:9200 | jq .  

3) Split the json dump into chunks less than 100MB each  
export DUMP=/path/to/enwiki-20250630-cirrussearch-content.json   #fill your path  
mkdir wiki-chunks && cd wiki-chunks  
split -a 6 -l 2000 "$DUMP" part_  

4) build the index:  
export ES=http://localhost:9200  
export INDEX=enwiki  

curl -s -H 'Content-Type: application/json' -XPUT "$ES/$INDEX" -d '{  
  "settings": {  
    "number_of_shards": 8,  
    "number_of_replicas": 0,  
    "refresh_interval": "-1",  
    "analysis": {  
      "analyzer": {  
        "wiki_en": {  
          "type": "custom",  
          "tokenizer": "standard",  
          "filter": ["lowercase","asciifolding","stop","porter_stem"]  
        }  
      }  
    }  
  },  
  "mappings": {  
    "dynamic_templates": [  
      { "strings": {  
          "match_mapping_type": "string",  
          "mapping": {  
            "type": "text",  
            "analyzer": "wiki_en",  
            "fields": { "keyword": { "type": "keyword", "ignore_above": 256 } }  
          }  
      } }  
    ],  
    "properties": {  
      "title": {  
        "type": "text",  
        "analyzer": "wiki_en",  
        "fields": { "keyword": { "type":"keyword","ignore_above":256 } }  
      },  
      "namespace": { "type": "integer" },  
      "timestamp": { "type": "date", "format": "strict_date_optional_time||epoch_millis" }  
    }  
  }  
}'  

5) load the data into the index - this step takes a while

rewrite() {  
  jq -c --arg idx "$INDEX" '  
    if has("index") then  
      .index._index = $idx | del(.index._type)  
    elif has("create") then  
      .create._index = $idx | del(.create._type)  
    elif has("delete") then  
      .delete._index = $idx | del(.delete._type)  
    else  
      .  
    end  
  '  
}  

for f in part_*; do  
  sz=$(wc -c < "$f")  
  [ "$sz" -gt 90000000 ] && { echo "SKIP too big: $f ($sz bytes)"; continue; }  

  echo "Indexing $f ($sz bytes)"  
  BODY=$(rewrite < "$f" | curl -sS --fail-with-body -w '\nHTTP_CODE=%{http_code}\n' \  
          -H 'Content-Type: application/x-ndjson' -XPOST "$ES/_bulk" --data-binary @-)  
  CODE=$(printf '%s\n' "$BODY" | awk -F= '/^HTTP_CODE=/{print $2}')  
  JSON=$(printf '%s\n' "$BODY" | sed '/^HTTP_CODE=/d')  

  if [ "$CODE" != "200" ]; then  
    echo "HTTP $CODE for $f"  
    printf '%s\n' "$JSON" | head -80  
    echo "ABORTING. Reduce chunk size or raise http.max_content_length and retry."  
    break  
  fi  

  printf '%s\n' "$JSON" | jq -r '"errors=" + (.errors|tostring) + " took=" + (.took|tostring) + "ms"'  
done  

6) Test the index - this should return around 7,000,000 articles  
curl -XPOST "$ES/$INDEX/_refresh"  
curl -s "$ES/$INDEX/_count" | jq .count  

Finally test pulling back some actual data:  
curl -s "$ES/$INDEX/_search" -H 'Content-Type: application/json' -d '{  
  "size": 8,  
  "_source": ["page_id","title","text","namespace"],  
  "query": {  
    "bool": {  
      "must": [  
        { "multi_match": {  
            "query": "origin of the Turing test",  
            "fields": ["title^8","opening_text^3","text"]  
        }}  
      ],  
      "filter": { "term": { "namespace": 0 } }  
    }  
  }  
}'  

6) point the openwebui tool at this instance  
