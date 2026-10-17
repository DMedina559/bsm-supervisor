const {test,expect}=require('@playwright/test');
const credential='browser-test-credential-0123456789';
async function login(page){await page.goto('/');await page.locator('#token').fill(credential);await page.locator('#login button').click();await expect(page.locator('#console')).toBeVisible();}
test('authentication, responsive layout, and lock',async({page,request})=>{
 const response=await request.get('/api/state');expect(response.status()).toBe(401);
 await login(page);await expect(page.locator('#selected')).toHaveText('No version selected');
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBeTruthy();
 await page.locator('#logout').click();await expect(page.locator('#console')).toBeHidden();await expect(page.locator('#login')).toBeVisible();
});
test('restore needs downtime and data confirmation; duplicate actions stay disabled',async({page})=>{
 await login(page);
 const selections=[{id:'a'.repeat(32)}];
 await page.route('**/api/state',route=>route.fulfill({json:{service:{ActiveState:'inactive'},selection:null,installations:[],checkpoints:selections,operations:[],jobs:[],schedules:[]}}));
 await page.locator('#refresh').click();await expect(page.locator('#checkpoint option')).toHaveCount(1);
 const dialogs=[];page.on('dialog',async dialog=>{dialogs.push(dialog.message());await dialog.accept();});
 let resolve,release;
 const payload=new Promise(done=>resolve=done);
 await page.route('**/api/actions',async route=>{resolve(route.request().postDataJSON());await new Promise(done=>{release=done;});await route.fulfill({status:202,json:{id:'b'.repeat(32),action:'checkpoint-restore'}});});
 await page.locator('[data-action="checkpoint-restore"]').click();
 const sent=await payload;expect(sent.restore_data).toBe(true);expect(sent.confirmed).toBe(true);expect(dialogs).toHaveLength(2);
 await expect(page.locator('[data-action="checkpoint-restore"]')).toBeDisabled();release();
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
