import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
import {createHash,createHmac} from 'node:crypto';

const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
const files=[
  'n8n/book-promotion-reel-publisher.json',
  'n8n/book-promotion-reel-prompt-helper.json',
];

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
    for(const outputs of Object.values(workflow.connections[node.name]||{})){
      for(const branch of outputs)for(const edge of branch){
        assert.ok(nodes.has(edge.node),`${file}: missing target ${edge.node}`);
      }
    }
    const serialized=JSON.stringify(node.parameters);
    for(const match of serialized.matchAll(/\$\('([^']+)'\)/g)){
      assert.ok(nodes.has(match[1]),`${file}: ${node.name} references missing ${match[1]}`);
    }
    const body=node.parameters?.jsonBody;
    if(typeof body==='string'&&body.includes('$')){
      assert.ok(body.startsWith('={{'),`${file}: malformed JSON body expression in ${node.name}`);
    }
  }
  console.log(`${file}: ${nodes.size} Nodes und Referenzen geprüft.`);
}

const publisher=JSON.parse(await readFile(files[0],'utf8'));
const publisherText=JSON.stringify(publisher).toLowerCase();
assert.ok(!publisherText.includes('langchain'),'nightly publisher must be AI-free');
assert.ok(!publisherText.includes('one.intelligence'),'nightly publisher must be AI-free');
assert.ok(publisherText.includes('bookpromo_reel_claim'));
assert.ok(publisherText.includes('publish_uncertain'));
assert.ok(publisherText.includes('bookpromo_reel_cleanup'));
assert.ok(!publisherText.includes('$env'));
assert.ok(publisherText.includes('r2_endpoint_hier_eintragen'));
assert.ok(publisherText.includes('r2_access_key_id_hier_eintragen'));
assert.ok(publisherText.includes('r2_secret_access_key_hier_eintragen'));
assert.ok(publisherText.includes('https://graph.instagram.com/refresh_access_token'));
assert.ok(publisherText.includes('ig_refresh_token'));
assert.ok(publisherText.includes('mit_richtigem_key_ersetzen'));

const signerNode=publisher.nodes.find(node=>node.name==='R2 URL context');
const signerConfig={
  r2_endpoint:'https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com',
  r2_access_key_id:'TESTACCESSKEY',r2_secret_access_key:'test-secret-key',
  r2_signed_url_ttl_seconds:86400,
};
const signerContext={asset:{storage_provider:'cloudflare_r2',storage_bucket:'test-bucket',storage_path:'folder/video test.mp4',public_url:null}};
const signed=await new AsyncFunction('$json','$',signerNode.parameters.jsCode)(
  signerContext,
  name=>{assert.equal(name,'Config');return {first:()=>({json:signerConfig})};},
);
const signedUrl=new URL(signed.json.signed_url);
const signature=signedUrl.searchParams.get('X-Amz-Signature');
const amz=signedUrl.searchParams.get('X-Amz-Date');
const canonicalQuery=signedUrl.search.slice(1).split('&').filter(value=>!value.startsWith('X-Amz-Signature=')).join('&');
const scope=`${amz.slice(0,8)}/auto/s3/aws4_request`;
const canonical=`GET\n${signedUrl.pathname}\n${canonicalQuery}\nhost:${signedUrl.host}\n\nhost\nUNSIGNED-PAYLOAD`;
const stringToSign=`AWS4-HMAC-SHA256\n${amz}\n${scope}\n${createHash('sha256').update(canonical).digest('hex')}`;
const h=(key,value)=>createHmac('sha256',key).update(value).digest();
const signingKey=h(h(h(h(`AWS4${signerConfig.r2_secret_access_key}`,amz.slice(0,8)),'auto'),'s3'),'aws4_request');
assert.equal(signature,createHmac('sha256',signingKey).update(stringToSign).digest('hex'),'R2 signer must produce a valid AWS v4 signature');
const signedDelete=await new AsyncFunction('$json','$',signerNode.parameters.jsCode)(
  {...signerContext,force_signed:true,signing_method:'DELETE'},
  name=>{assert.equal(name,'Config');return {first:()=>({json:signerConfig})};},
);
const deleteUrl=new URL(signedDelete.json.signed_url);
const deleteAmz=deleteUrl.searchParams.get('X-Amz-Date');
const deleteQuery=deleteUrl.search.slice(1).split('&').filter(value=>!value.startsWith('X-Amz-Signature=')).join('&');
const deleteScope=`${deleteAmz.slice(0,8)}/auto/s3/aws4_request`;
const deleteCanonical=`DELETE\n${deleteUrl.pathname}\n${deleteQuery}\nhost:${deleteUrl.host}\n\nhost\nUNSIGNED-PAYLOAD`;
const deleteString=`AWS4-HMAC-SHA256\n${deleteAmz}\n${deleteScope}\n${createHash('sha256').update(deleteCanonical).digest('hex')}`;
const deleteKey=h(h(h(h(`AWS4${signerConfig.r2_secret_access_key}`,deleteAmz.slice(0,8)),'auto'),'s3'),'aws4_request');
assert.equal(deleteUrl.searchParams.get('X-Amz-Signature'),createHmac('sha256',deleteKey).update(deleteString).digest('hex'),'R2 DELETE signer must produce a valid AWS v4 signature');
assert.equal(signedDelete.json.url_kind,'r2_signed_delete');

const instagramNodes=['Create Instagram Reel','Instagram Reel status','Publish Instagram Reel'];
for(const name of instagramNodes){
  const node=publisher.nodes.find(candidate=>candidate.name===name);
  assert.ok(node,`missing ${name}`);
  assert.equal(node.credentials,undefined,`${name} must use refreshed execution token`);
  assert.ok(JSON.stringify(node.parameters.queryParameters).includes("$('Instagram access token').first().json.access_token"));
}

const helper=JSON.parse(await readFile(files[1],'utf8'));
const webhook=helper.nodes.find(node=>node.name==='Authenticated prompt webhook');
assert.equal(webhook.parameters.authentication,'headerAuth');
assert.ok(webhook.credentials.httpHeaderAuth.id.startsWith('REPLACE_'));
