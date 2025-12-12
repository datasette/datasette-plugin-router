
spec.json:
	uv run \
    python -c "from plugin import router; import json; print(json.dumps(router.openapi_document_json()))" > $@

generated_client: spec.json openapi-ts.config.ts
	rm -rf $@
	mkdir $@
	npx @hey-api/openapi-ts -f openapi-ts.config.ts -i $< -o $@

script.js: script.ts generated_client
	rm -f $@ || true
	npx esbuild $< --bundle --minify --outfile=$@

clean:
	rm -f spec.json
	rm -rf generated_client
	rm -f script.js
.PHONY: spec.json generated_client clean