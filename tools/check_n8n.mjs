import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';

const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
const files=['n8n/book-promotion-review.json','n8n/book-promotion-auto.json'];
const workflows=[];

for(const file of files){
 const workflow=JSON.parse(await readFile(file,'utf8'));
 const nodes=new Map(workflow.nodes.map(node=>[node.name,node]));
 assert.equal(nodes.size,workflow.nodes.length,`${file}: duplicate node names`);
 assert.equal(workflow.active,false);
 assert.equal(workflow.settings.timezone,'Europe/Berlin');
 assert.equal(workflow.settings.saveDataErrorExecution,'none');
 assert.equal(workflow.settings.saveDataSuccessExecution,'none');
 for(const node of workflow.nodes){
  if(node.type==='n8n-nodes-base.code'){
   new AsyncFunction('$input','$','$execution',node.parameters.jsCode);
  }
  for(const credential of Object.values(node.credentials||{})){
   assert.ok(credential.id.startsWith('REPLACE_'),`${file}: real credential id in ${node.name}`);
  }
  const sourceConnections=workflow.connections[node.name]||{};
  for(const outputs of Object.values(sourceConnections)){
   for(const branch of outputs)for(const edge of branch){
    assert.ok(nodes.has(edge.node),`${file}: missing target ${edge.node}`);
   }
  }
  const serialized=JSON.stringify(node.parameters);
  for(const match of serialized.matchAll(/\$\('([^']+)'\)/g)){
   assert.ok(nodes.has(match[1]),`${file}: ${node.name} references missing ${match[1]}`);
  }
 }
 const serialized=JSON.stringify(workflow);
 assert.ok(!serialized.toLowerCase().includes('uguu'));
 assert.ok(!serialized.includes('Bearer '));
 assert.equal((serialized.match(/MIT_RICHTIGEM_KEY_ERSETZEN/g)||[]).length,2);
 assert.ok(serialized.includes('/storage/v1/object/sign/'));
 for(const node of workflow.nodes){
  const body=node.parameters?.jsonBody;
  if(typeof body==='string'&&body.includes('$')){
   assert.ok(body.startsWith('={{'),`${file}: malformed JSON body expression in ${node.name}`);
   assert.ok(!body.startsWith('={ {'),`${file}: split expression braces in ${node.name}`);
  }
 }
 workflows.push({file,workflow,nodes});
}

const [review,auto]=workflows;
for(const name of ['Telegram decisions','Approve text','Show carousel album','Approve carousel']){
 assert.ok(review.nodes.has(name),`review missing ${name}`);
 assert.ok(!auto.nodes.has(name),`auto must not contain ${name}`);
}
assert.equal(review.nodes.get('Show carousel album').parameters.operation,'sendMediaGroup');

for(const {file,nodes} of workflows){
 const split=nodes.get('Split quote into slides').parameters.jsCode;
 assert.ok(split.includes("new Intl.Segmenter('de',{granularity:'sentence'})"));
 assert.ok(split.includes('slides.length>8'));
 assert.ok(split.includes('Quote integrity check failed'));
 const splitFn=new AsyncFunction('$input','$','$execution',split);
 const quoteText='Der erste Satz bleibt vollständig. Der zweite Satz ist ebenfalls unverändert.\n\nEin neuer Absatz bleibt erkennbar.';
 const splitResult=await splitFn.call({},
  {first:()=>({json:{quote_text:quoteText},binary:{data:{id:'base'}}})},()=>{},{});
 assert.equal(splitResult.map(x=>x.json.slide_text).join(' ').replace(/\s+/g,' ').trim(),quoteText.replace(/\s+/g,' ').trim());
 assert.ok(splitResult.length>=1&&splitResult.length<=8);
 assert.ok(splitResult.every(x=>x.json.render_text.split('\n').every(line=>[...line].length<=30)));
 const quote=nodes.get('Add quote text').parameters;
 assert.equal(quote.options.font,'/usr/share/fonts/truetype/msttcorefonts/Arial.ttf');
 assert.equal(quote.fontColor,'#FFFFFF');
 assert.equal(quote.text,'={{ $json.render_text }}');
 assert.equal(quote.lineLength,200);
 assert.equal(nodes.get('Quote label panel').parameters.color,'#11111133');
 const quoteSlideCode=nodes.get('Quote slide').parameters.jsCode;
 const quoteSlideFn=new AsyncFunction('$input','$','$execution',quoteSlideCode);
 const quoteSlideInput=[1,2].map(slide_number=>({
  json:{slide_number,slide_count:2,slide_text:`Teil ${slide_number}`,book_profile:{title:'Testbuch'}},
  binary:{data:{id:`quote-${slide_number}`}},
 }));
 const quoteSlideResult=await quoteSlideFn.call({}, {all:()=>quoteSlideInput},()=>{},{});
 assert.equal(quoteSlideResult.length,2);
 assert.deepEqual(quoteSlideResult.map(x=>x.json.position),[1,2]);
 assert.deepEqual(quoteSlideResult.map(x=>x.binary.data.fileName),['01-quote.jpg','02-quote.jpg']);
 const manifest=nodes.get('Final carousel manifest').parameters.jsCode;
 assert.ok(manifest.includes('items.length<3||items.length>10'));
 assert.ok(manifest.includes('sha256(bytes)'));
 const jpeg=Buffer.from([255,216,255,192,0,17,8,5,70,4,56,3,1,17,0,2,17,0,3,17,0]);
 const media=['hero','quote','cta'].map((kind,position)=>({
  json:{id:'12345678-1234-1234-1234-123456789012',revision:7,position,kind,
   text_fragment:kind==='quote'?'Zitat':null,alt_text:kind},
  binary:{data:{id:`media-${position}`,mimeType:'image/jpeg'}},
 }));
 const manifestFn=new AsyncFunction('$input','$','$execution',manifest);
 const manifestResult=await manifestFn.call({helpers:{getBinaryDataBuffer:async()=>jpeg}},
  {all:()=>media},()=>{},{});
 const digest=createHash('sha256').update(jpeg).digest('hex');
 assert.equal(manifestResult[0].json.sha256,digest);
 assert.ok(manifestResult[2].json.storage_path.endsWith(`/02-${digest}.jpg`));
 const child=Object.fromEntries(nodes.get('Create Instagram child').parameters.bodyParameters.parameters.map(x=>[x.name,x.value]));
 assert.deepEqual(Object.keys(child).sort(),['alt_text','image_url','is_carousel_item']);
 const parent=Object.fromEntries(nodes.get('Create Instagram parent').parameters.bodyParameters.parameters.map(x=>[x.name,x.value]));
 assert.equal(parent.media_type,'CAROUSEL');
 assert.equal(parent.is_ai_generated,'true');
 assert.ok(parent.caption&&parent.children);
 assert.ok(nodes.get('Save child container').parameters.url.includes('bookpromo_media_container'));
 assert.ok(nodes.get('Save media cleanup').parameters.url.includes('bookpromo_media_cleanup'));
 assert.equal(nodes.get('Delete temporary media').parameters.method,'DELETE');
 assert.ok(nodes.get('Config').parameters.jsCode.includes('publish_enabled:false'));
 const refresh=Object.fromEntries(nodes.get('Refresh token for insta').parameters.queryParameters.parameters.map(x=>[x.name,x.value]));
 assert.deepEqual(refresh,{grant_type:'ig_refresh_token',access_token:'MIT_RICHTIGEM_KEY_ERSETZEN'});
 for(const name of ['Create Instagram child','Instagram child status','Create Instagram parent','Instagram parent status','Publish Instagram carousel','Read Instagram permalink']){
  const token=nodes.get(name).parameters.queryParameters.parameters.filter(x=>x.name==='access_token');
  assert.deepEqual(token,[{name:'access_token',value:"={{ $('Instagram access token').first().json.access_token }}"}]);
 }
 console.log(`${file}: ${nodes.size} Nodes und Carousel-Vertrag geprüft.`);
}
