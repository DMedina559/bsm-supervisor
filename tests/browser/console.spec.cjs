const {test,expect}=require('@playwright/test');
const credential='browser-test-credential-0123456789';
async function login(page){await page.goto('/');await page.locator('#token').fill(credential);await page.locator('#login button').click();await expect(page.locator('#console')).toBeVisible();}
test('authentication, responsive layout, and lock',async({page,request})=>{
 const response=await request.get('/api/state');expect(response.status()).toBe(401);
 await login(page);await expect(page.locator('#selected')).toHaveText('No version selected');
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
 await page.locator('#logout').click();await expect(page.locator('#console')).toBeHidden();await expect(page.locator('#login')).toBeVisible();
});
test('restore wizard requires inspection and confirmation and prevents duplicate submission',async({page})=>{
 await login(page);
 const id='a'.repeat(32),digest='c'.repeat(64);
 await page.route('**/api/state',route=>route.fulfill({json:{service:{ActiveState:'inactive'},selection:null,installations:[],checkpoints:[{id}],operations:[],jobs:[],schedules:[]}}));
 await page.route('**/api/checkpoints/'+id,route=>route.fulfill({json:{id,digest,compatible_version:true,scope:'Full BSM data',database_path:'/srv/bsm/config/bedrock-server-manager.db'}}));
 await page.locator('#refresh').click();await expect(page.locator('#checkpoint option')).toHaveCount(1);
 await page.locator('#restore-open').click();await expect(page.locator('#restore-dialog')).toBeVisible();
 await expect(page.locator('#restore-details')).toContainText('bedrock-server-manager.db');
 await page.locator('#restore-confirm-button').click();await expect(page.locator('#error')).toContainText('Confirm downtime');
 let resolve,release;const payload=new Promise(done=>resolve=done);
 await page.route('**/api/actions',async route=>{resolve(route.request().postDataJSON());await new Promise(done=>{release=done;});await route.fulfill({status:202,json:{id:'b'.repeat(32),action:'backup-restore'}});});
 await page.locator('#restore-confirm').check();await page.locator('#restore-confirm-button').click();
 const sent=await payload;expect(sent).toMatchObject({action:'backup-restore',target:id,digest,confirmed:true,restart_after:false});
 await expect(page.locator('#restore-open')).toBeDisabled();release();
});
test('locking discards delayed progress',async({page})=>{
 await login(page);
 let complete;const started=new Promise(resolve=>complete=resolve);
 await page.route('**/api/events*',async route=>{complete();await new Promise(resolve=>setTimeout(resolve,400));try{await route.fulfill({json:[]});}catch{}});
 await page.locator('#refresh').click();await started;await page.locator('#logout').click();
 await page.waitForTimeout(600);await expect(page.locator('#console')).toBeHidden();await expect(page.locator('#login')).toBeVisible();
});

test('HTTPS proxy origin preserves authenticated access',async({page})=>{
 await page.route('https://supervisor.example/**',async route=>{
   const request=route.request();const url=new URL(request.url());
   const response=await route.fetch({url:'http://127.0.0.1:11327'+url.pathname+url.search,headers:{...request.headers(),host:'supervisor.example'}});
   await route.fulfill({response});
 });
 await page.goto('https://supervisor.example/');await page.locator('#token').fill(credential);await page.locator('#login button').click();
 await expect(page.locator('#console')).toBeVisible();await expect(page.locator('#selected')).toHaveText('No version selected');
});

test('bundled frontend branding and readable service state',async({page})=>{
 await login(page);
 const logo=page.locator('.brand img');
 await expect(logo).toBeVisible();
 expect(await logo.evaluate(img=>img.complete && img.naturalWidth===192)).toBeTruthy();
 expect(await page.evaluate(()=>getComputedStyle(document.documentElement).getPropertyValue('--bsm-accent').trim())).toBe('#00ed95');
 expect(await page.locator('[data-action="start"]').evaluate(button=>getComputedStyle(button).borderRadius)).toBe('10px');
 await expect(page.locator('#service')).not.toContainText('ActiveState');
 await page.screenshot({path:test.info().outputPath('console.png'),fullPage:true});
});

test('invalid installation IDs receive actionable validation without queued jobs',async({page})=>{
 await login(page);
 await page.locator('#version').fill('release-4.0.0b7');
 await page.locator('[data-action="install"]').click();
 await expect(page.locator('#error')).toContainText('Choose stable, prerelease, or an exact version');
 await expect(page.locator('#error')).toContainText('Select while stopped');
 await expect(page.locator('#jobs details')).toHaveCount(0);
});

test('administration sections, settings and schedule forms are responsive',async({page})=>{
 await login(page);
 for(const name of ['backups','automation','settings','system']){
  await page.locator('[data-section="'+name+'"]').click();await expect(page.locator('#page-'+name)).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
  await page.screenshot({path:test.info().outputPath(name+'.png'),fullPage:true});
 }
 await page.locator('[data-section="automation"]').click();await page.locator('#schedule-name').fill('nightly-backup');
 await page.locator('#schedule-action').selectOption('checkpoint');await page.locator('#schedule-maintenance').check();
 page.on('dialog',dialog=>dialog.accept());
 const payload=page.waitForRequest(request=>request.url().endsWith('/api/actions'));
 await page.route('**/api/actions',route=>route.fulfill({status:202,json:{id:'d'.repeat(32),action:'schedule-save'}}));
 await page.locator('#schedule-form button').click();expect((await payload).postDataJSON()).toMatchObject({action:'schedule-save',policy:{name:'nightly-backup',action:'checkpoint',maintenance_confirmed:true,days:[1,2,3,4,5,6,7]}});
});

test('browser archive upload and authenticated download use opaque transfer IDs',async({page})=>{
 await login(page);await page.locator('[data-section="backups"]').click();
 const id='e'.repeat(32);
 await page.route('**/api/transfers',async route=>{expect(route.request().headers()['authorization']).toBe('Bearer '+credential);await route.fulfill({status:201,json:{id,bytes:12}});});
 await page.locator('#archive-file').setInputFiles({name:'backup.bsmcheckpoint',mimeType:'application/octet-stream',buffer:Buffer.from('archive data')});
 await page.locator('#backup-upload').click();await expect(page.locator('#transfer-status')).toContainText('Upload complete');
 await page.locator('#archive-password').fill('private password value');await page.locator('[data-section="system"]').click();await page.locator('#github-token').fill('github_secret');
 await page.locator('#logout').click();await expect(page.locator('#archive-password')).toHaveValue('');await expect(page.locator('#github-token')).toHaveValue('');
});
