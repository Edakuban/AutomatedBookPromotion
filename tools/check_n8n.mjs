import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
const w=JSON.parse(await readFile('n8n/book-promotion.json','utf8'));
const nodes=new Map(w.nodes.map(n=>[n.name,n]));
const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
assert.equal(nodes.size,w.nodes.length);
assert.equal(w.active,false);
assert.equal(w.settings.timezone,'Europe/Berlin');
for(const n of w.nodes){
 if(n.type==='n8n-nodes-base.code') {
  new AsyncFunction('$input','$','$execution',n.parameters.jsCode);
  assert.equal(n.parameters.mode,'runOnceForAllItems');
  assert.ok(!n.parameters.jsCode.includes('$json'));
 }
 for(const credentials of Object.values(n.credentials||{}))assert.ok(credentials.id.startsWith('REPLACE_'));
 for(const items of Object.values(w.connections[n.name]||{}))for(const branch of items)for(const item of branch)assert.ok(nodes.has(item.node));
}
const evaluate=(name,input,refs={},mode='production')=>new Function('$input','$','$execution',nodes.get(name).parameters.jsCode)({all:()=>[{json:input}],first:()=>({json:input})},key=>({item:{json:refs[key]},first:()=>({json:refs[key]})}),{mode});
assert.equal(evaluate('Current draft',{outcome:'existing',post:{status:'generating_text'}}).length,0);
assert.equal(evaluate('Current draft',{outcome:'existing',post:{status:'generating_image'}}).length,0);
assert.equal(evaluate('Current draft',{outcome:'existing',post:{status:'awaiting_text_approval'}})[0].json.status,'awaiting_text_approval');
assert.equal(evaluate('Current draft',{outcome:'stale'}).length,0);
assert.throws(()=>evaluate('No quote context',{outcome:'inactive'}),/Promotion deaktiviert/);
assert.equal(evaluate('No quote context',{outcome:'existing'}).length,0);
assert.equal(evaluate('No quote context',{outcome:'no_quote'},{Config:{telegram_chat_id:'test'}})[0].json.telegram_chat_id,'test');
const uuid='12345678-1234-1234-1234-123456789012';
const cb={data:uuid+':4:t',message:{chat:{id:42}},from:{id:7},id:'callback'};
const parsed=evaluate('Parse callback',{callback_query:cb})[0].json;
assert.equal(parsed.action,'approve_text');assert.equal(parsed.user_id,'7');
assert.equal(evaluate('Parse callback',{callback_query:{...cb,data:'other'}}).length,0);
assert.equal(evaluate('Decision request',{id:uuid,revision:5},{'Parse callback':parsed}).length,0);
const d={id:uuid,revision:4,action_token:'token',quote_text:'Ein wortgetreues Zitat.',book_profile:{title:'Buch',author:'Autor'}};
const result=evaluate('Validate caption',{addition:'Ein Blick ins Buch.',image_prompt:'Nacht'},{'Text request':d});
assert.ok(result[0].json.p_data.caption.startsWith(d.quote_text+'\n\n'));
assert.deepEqual(evaluate('Validate caption',{output:{addition:'Ein Blick ins Buch.',image_prompt:'Nacht'}},{'Text request':d}),result);
assert.deepEqual(evaluate('Validate caption',{result:{response:'{"addition":"Ein Blick ins Buch.","image_prompt":"Nacht"}'},success:true},{'Text request':d}),result);
for(const invalid of [{output:null},{output:'{"addition":"Text"}'},{output:[]},{output:{addition:'Text',image_prompt:'Nacht',extra:true}},{output:{addition:'Text',image_prompt:'Nacht'},extra:true}]) {
 assert.throws(()=>evaluate('Validate caption',invalid,{'Text request':d}));
}
for(const invalid of [{addition:'Text'}, {addition:'Text',image_prompt:''}, {addition:'x'.repeat(2200),image_prompt:'Nacht'}, {addition:'Text',image_prompt:'x'.repeat(4001)}, {addition:'Text',image_prompt:'Nacht',tool_calls:[{}]}, {text:'{"addition":"abgeschnitten'}]) {
 assert.throws(()=>evaluate('Validate caption',invalid,{'Text request':d}));
}
const binary={data:'filesystem-v2',id:'filesystem-v2:synthetic-image',mimeType:'image/jpeg'};
const imageBinary=(buffer,item={binary:{data:binary}})=>new AsyncFunction('$input','$',nodes.get('Image binary').parameters.jsCode).call(
 {helpers:{getBinaryDataBuffer:async(index,key)=>{assert.equal(index,0);assert.equal(key,'data');return buffer;}}},
 {first:()=>item},key=>{throw new Error('Unexpected reference '+key);});
const baseImage=(buffer,item={binary:{data:binary}})=>new AsyncFunction('$input','$',nodes.get('Base image binary').parameters.jsCode).call(
 {helpers:{getBinaryDataBuffer:async(index,key)=>{assert.equal(index,0);assert.equal(key,'data');return buffer;}}},
 {first:()=>item},key=>{assert.equal(key,'Image request');return {item:{json:d}};});
// Minimal JPEG header with a SOF0 segment declaring 1080x1350. The workflow
// only inspects the signature and dimensions here; image decoding is covered
// by the separate ImageMagick check.
const jpeg=Buffer.from([255,216,255,192,0,17,8,5,70,4,56,3,1,17,0,2,17,0,3,17,0]);
const baseResult=(await baseImage(jpeg))[0];
assert.equal(baseResult.binary.data.mimeType,'image/jpeg');
assert.equal(baseResult.binary.data.id,binary.id);
assert.equal(baseResult.json.id,d.id);
const imageResult=(await imageBinary(jpeg,{binary:{data:binary},json:d}))[0];
assert.equal(imageResult.binary.data.mimeType,'image/jpeg');
assert.equal(imageResult.json.id,d.id);
const oversized=Buffer.alloc(8388609);oversized.set([255,216,255]);
await assert.rejects(baseImage(oversized));
await assert.rejects(baseImage(Buffer.from([137,80,78,71])));
await assert.rejects(baseImage(jpeg,{}));
assert.equal(evaluate('Validate title overlay',d)[0].json.overlay_path,'');
const overlayPath=uuid+'/'+'a'.repeat(64)+'.png';
assert.equal(evaluate('Validate title overlay',{...d,book_profile:{...d.book_profile,overlay_path:overlayPath}})[0].json.overlay_path,overlayPath);
assert.throws(()=>evaluate('Validate title overlay',{...d,book_profile:{overlay_path:'../../private'}}));
assert.equal(evaluate('Image ready request',{success:true,files:[{url:'https://example.com/image.jpg'}]},{'Image request':d})[0].json.p_data.image_url,'https://example.com/image.jpg');
assert.throws(()=>evaluate('Validate caption',{choices:[{finish_reason:'length'}]},{'Text request':d}));
assert.throws(()=>evaluate('Image request',d,{'Config':{image_model:'test'}}));
assert.throws(()=>evaluate('Resume preview',{status:'publishing'}));
for(const status of ['generating_text','generating_image']) {
 assert.throws(()=>evaluate('Resume preview',{status},{Config:{resume_generation:false}},'test'));
 assert.throws(()=>evaluate('Resume preview',{status},{Config:{resume_generation:true}},'production'));
 assert.equal(evaluate('Resume preview',{status},{Config:{resume_generation:true}},'test')[0].json.post.status,status);
}
for(const status of ['awaiting_text_approval','awaiting_image_approval','approved']) {
 assert.equal(evaluate('Resume preview',{status},{Config:{resume_generation:false}},'test')[0].json.outcome,'updated');
}
for(const status of ['published','discarded','failed','publish_uncertain']) {
 assert.throws(()=>evaluate('Resume preview',{status},{Config:{resume_generation:true}},'test'));
}
assert.equal(evaluate('Config',{})[0].json.resume_generation,false);
// n8n exposes test/production here, not its internal workflow run-mode names.
const resumeExpression=nodes.get('Manual resume?').parameters.conditions.conditions[0].leftValue;
const shouldResume=(mode,id)=>new Function('$json','$execution',
 'return ('+resumeExpression.slice(3,-2).trim()+');')({resume_post_id:id},{mode});
assert.equal(shouldResume('test',uuid),true);
assert.equal(shouldResume('test',''),false);
assert.equal(shouldResume('production',uuid),false);
assert.equal(shouldResume('manual',uuid),false);
assert.equal(w.connections['Manual resume?'].main[0][0].node,'Read resume draft');
assert.equal(w.connections['Read resume draft'].main[0][0].node,'Resume preview');
assert.equal(w.connections['Resume preview'].main[0][0].node,'Current draft');
const resumed=evaluate('Resume preview',{...d,status:'generating_text'},
 {Config:{resume_generation:true}},'test')[0].json;
assert.equal(evaluate('Current draft',resumed)[0].json.status,'generating_text');
assert.equal(nodes.get('Draft available?').parameters.conditions.conditions[0].leftValue,"={{ ['created','existing'].includes($json.outcome) }}");
assert.equal(w.connections['Reserve quote'].main[0][0].node,'Draft available?');
assert.equal(w.connections['Draft available?'].main[0][0].node,'Current draft');
assert.equal(nodes.get('Generate caption').type,'@n8n/n8n-nodes-langchain.chainLlm');
assert.equal(nodes.get('Generate caption').parameters.hasOutputParser,true);
assert.equal(nodes.get('one.intelligence Chat Model').type,'CUSTOM.lmChatOneIntelligence');
assert.ok(nodes.get('one.intelligence Chat Model').credentials.oneIntelligenceApi);
assert.equal(w.connections['one.intelligence Chat Model'].ai_languageModel[0][0].node,'Generate caption');
assert.equal(w.connections['Caption output parser'].ai_outputParser[0][0].node,'Generate caption');
const parser=nodes.get('Caption output parser').parameters;
assert.equal(parser.autoFix,false);
assert.deepEqual(JSON.parse(parser.inputSchema).required,['addition','image_prompt']);
assert.equal(JSON.parse(parser.inputSchema).additionalProperties,false);
for(const name of ['Read draft','Read resume draft']) {
 const n=nodes.get(name);
 assert.equal(n.type,'n8n-nodes-base.supabase');
 assert.equal(n.parameters.operation,'get');
 assert.equal(n.parameters.tableId,'posts');
 assert.deepEqual(n.parameters.filters.conditions.map(c=>c.keyName),['id','account_id']);
 assert.equal(n.credentials.supabaseApi.id,nodes.get('Reserve quote').credentials.supabaseApi.id);
}
for(const name of ['Reserve quote','Save transition','Claim publishing','Save container']) {
 const n=nodes.get(name);
 assert.equal(n.type,'n8n-nodes-base.httpRequest');
 assert.equal(n.parameters.authentication,'predefinedCredentialType');
 assert.equal(n.parameters.nodeCredentialType,'supabaseApi');
 assert.ok(n.parameters.url.includes('/rest/v1/rpc/'));
}
const imageNode=nodes.get('Generate image');
assert.equal(imageNode.type,'@n8n/n8n-nodes-langchain.openAi');
assert.equal(imageNode.parameters.operation,'generate');
assert.equal(imageNode.parameters.resource,'image');
assert.equal(imageNode.parameters.modelId.mode,'id');
assert.deepEqual(w.connections['Image request'].main[0].map(x=>x.node),['Cloudflare FLUX image']);
assert.equal(nodes.get('Cloudflare FLUX image').credentials.httpHeaderAuth.id,'REPLACE_httpHeaderAuth');
assert.equal(nodes.get('Cloudflare caption').credentials.httpHeaderAuth.id,'REPLACE_httpHeaderAuth');
assert.equal(w.connections['Cloudflare FLUX image'].main[0][0].node,'Convert to File');
assert.equal(w.connections['Cloudflare FLUX image'].main[1][0].node,'Generate image failed');
assert.equal(w.connections['Convert to File'].main[0][0].node,'Convert to JPEG');
assert.equal(w.connections['Convert to File'].main[1][0].node,'Generate image failed');
assert.deepEqual(w.connections['Text request'].main[0].map(x=>x.node),['Generate caption']);
assert.equal(w.connections['Cloudflare caption'].main[0][0].node,'Validate caption');
assert.equal(w.connections['Cloudflare caption'].main[1][0].node,'Generate caption failed');
assert.equal(nodes.get('Convert to JPEG').parameters.options.format,'jpeg');
assert.equal(w.connections['Generate image'].main[0][0].node,'Convert to JPEG');
assert.equal(w.connections['Convert to JPEG'].main[0][0].node,'Crop to 4:5');
assert.equal(w.connections['Crop to 4:5'].main[0][0].node,'Resize to 1080x1350');
assert.equal(w.connections['Resize to 1080x1350'].main[0][0].node,'Base image binary');
assert.equal(w.connections['Base image binary'].main[0][0].node,'Validate title overlay');
assert.equal(w.connections['Validate title overlay'].main[0][0].node,'Overlay configured?');
assert.equal(w.connections['Overlay configured?'].main[0][0].node,'Download title overlay');
assert.equal(w.connections['Overlay configured?'].main[1][0].node,'Chapter label panel');
assert.equal(w.connections['Download title overlay'].main[0][0].node,'Assemble image layers');
assert.equal(w.connections['Assemble image layers'].main[0][0].node,'Composite title overlay');
assert.equal(w.connections['Composite title overlay'].main[0][0].node,'Chapter label panel');
assert.equal(w.connections['Chapter label panel'].main[0][0].node,'Add chapter label');
assert.equal(w.connections['Add chapter label'].main[0][0].node,'Image binary');
const download=nodes.get('Download title overlay');
assert.equal(download.parameters.authentication,'predefinedCredentialType');
assert.equal(download.parameters.nodeCredentialType,'supabaseApi');
assert.equal(download.parameters.responseFormat,'file');
assert.equal(download.parameters.outputPropertyName,'overlay');
assert.ok(download.parameters.url.includes('/storage/v1/object/authenticated/book-promotion-assets/'));
const composite=nodes.get('Composite title overlay');
assert.equal(composite.parameters.operation,'composite');
assert.equal(composite.parameters.dataPropertyNameComposite,'overlay');
assert.equal(composite.parameters.operator,'Over');
assert.equal(nodes.get('Chapter label panel').parameters.operation,'draw');
assert.equal(nodes.get('Add chapter label').parameters.operation,'text');
assert.ok(nodes.get('Add chapter label').parameters.text.includes('chapter_position'));
for(const name of ['Generate caption','Generate image','Convert to JPEG','Crop to 4:5','Resize to 1080x1350','Base image binary','Validate title overlay','Download title overlay','Assemble image layers','Composite title overlay','Chapter label panel','Add chapter label','Image binary']) {
 assert.equal(nodes.get(name).onError,'continueErrorOutput');
 assert.equal(nodes.get(name).retryOnFail,undefined);
 assert.equal(w.connections[name].main[1][0].node,name+' failed');
 assert.equal(evaluate(name+' failed',{}, {[name==='Generate caption'?'Text request':'Image request']:d})[0].json.p_action,'fail');
}
for(const name of ['Instagram container','Container status','Publish Instagram','Refresh token for insta']) {
 const n=nodes.get(name);
 assert.equal(n.credentials,undefined);
 assert.equal(n.parameters.authentication,undefined);
 const params=(n.parameters.bodyParameters||n.parameters.queryParameters).parameters;
 const token=params.find(p=>p.name==='access_token');
 assert.ok(token);
 assert.equal(token.value,name==='Refresh token for insta'?'REPLACE_INSTAGRAM_LONG_LIVED_TOKEN':"={{ $('Instagram access token').first().json.access_token }}");
}
assert.throws(()=>evaluate('Instagram access token',{}));
assert.throws(()=>evaluate('Instagram access token',{access_token:'REPLACE_TOKEN'}));
assert.equal(evaluate('Instagram access token',{access_token:'synthetic_test_token'})[0].json.access_token,'synthetic_test_token');
assert.equal(w.connections['Publication gate'].main[0][0].node,'Refresh token for insta');
assert.equal(w.connections['Publication gate'].main[1][0].node,'Publication paused');
assert.equal(w.connections['Refresh token for insta'].main[0][0].node,'Instagram access token');
assert.equal(w.connections['Instagram access token'].main[0][0].node,'Begin publishing');
assert.deepEqual(Object.keys(evaluate('Begin publishing',{}, {'Publication gate':d})[0].json).sort(),['p_action','p_data','p_id','p_revision','p_token']);
console.log(`n8n: ${nodes.size} Nodes, Graph, JavaScript, Credential-Trennung und Freigabeschutz geprüft.`);
