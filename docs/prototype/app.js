/* ==========================================================================
   All Pro Charter — Lead Manager  ·  prototype data store + logic
   --------------------------------------------------------------------------
   Design prototype only. All data below is hard-coded sample data and lives
   in memory (it resets on reload). The production build is Django + DRF with
   this same UX: Tailwind + Alpine on the front end, Zapier REST Hooks into
   LimoAnywhere, and Podium's API for messaging.

   Domain model
   ------------
   Lead = one Quote. A Quote holds many Reservations (priced line items).
   Reservation.tripType is 'transfer' (flat base rate) or 'hourly'
   (max(hours, minHours) x hourlyRate). Reservation.stops is an ordered route:
   first = pickup, last = drop-off, anything between = a stop (multi-stop).
   On "Mark booked", each reservation becomes a LimoAnywhere reservation.
   ========================================================================== */

function app() {
  return {
    /* ----------------------------------------------------------------- nav */
    view: 'leads',          // leads | workspace | inbox | pipeline | contacts | reviews | settings
    navOpen: true,          // sidebar expanded (desktop) / drawer (mobile)

    /* ----------------------------------------------------------- list state */
    filter: 'All',          // All | New | Quoted | Booked | Lost
    channelFilter: 'All',
    q: '',
    sortKey: 'recent',
    sortDir: -1,

    /* ------------------------------------------------------- workspace state */
    selectedId: null,

    /* -------------------------------------------------- reservation editor */
    editorOpen: false,
    draft: null,
    draftIsNew: false,

    /* ------------------------------------------------------- booking sync */
    syncOpen: false,
    syncDone: false,
    syncLeadId: null,
    syncSteps: [],
    laCounter: 5120,

    /* --------------------------------------------------------------- inbox */
    convoId: null,
    convoQ: '',
    replyText: '',

    /* ------------------------------------------------------------- toasts */
    toasts: [],
    toastSeq: 0,

    /* ------------------------------------------------------- notifications */
    notifOpen: false,
    notifications: [],
    notifSeq: 0,

    /* ------------------------------------------------------------ payments */
    depositPct: 50,

    /* ----------------------------------------------------- pipeline DnD */
    draggingId: null,
    dragOver: null,

    /* =================================================================== */
    /*  Reference data                                                     */
    /* =================================================================== */
    vehicles: [
      'Luxury Sedan', 'Luxury SUV', 'Sprinter Van (14)',
      'Mini Coach (28)', 'Motor Coach (55)', '2× Motor Coach (55)', 'Stretch Limousine'
    ],

    channelMeta: {
      'Website':     { label:'Website',     icon:'ti-world',         dot:'bg-sky-500',     pill:'bg-sky-50 text-sky-700 ring-sky-600/15',          avatar:'bg-sky-100 text-sky-800',     ring:'ring-sky-400/40' },
      'Wedding Pro': { label:'Wedding Pro',  icon:'ti-heart',         dot:'bg-rose-500',    pill:'bg-rose-50 text-rose-700 ring-rose-600/15',       avatar:'bg-rose-100 text-rose-800',   ring:'ring-rose-400/40' },
      'Phone':       { label:'Phone',        icon:'ti-phone',         dot:'bg-emerald-500', pill:'bg-emerald-50 text-emerald-700 ring-emerald-600/15', avatar:'bg-emerald-100 text-emerald-800', ring:'ring-emerald-400/40' },
      'API':         { label:'API',          icon:'ti-plug-connected',dot:'bg-violet-500',  pill:'bg-violet-50 text-violet-700 ring-violet-600/15', avatar:'bg-violet-100 text-violet-800',ring:'ring-violet-400/40' },
    },

    statusMeta: {
      New:    { label:'New',    pill:'bg-sky-50 text-sky-700 ring-sky-600/20',         dot:'bg-sky-500',     bar:'bg-sky-500' },
      Quoted: { label:'Quoted', pill:'bg-amber-50 text-amber-800 ring-amber-600/20',   dot:'bg-amber-500',   bar:'bg-amber-500' },
      Booked: { label:'Booked', pill:'bg-emerald-50 text-emerald-700 ring-emerald-600/20', dot:'bg-emerald-500', bar:'bg-emerald-500' },
      Lost:   { label:'Lost',   pill:'bg-slate-100 text-slate-500 ring-slate-500/15',  dot:'bg-slate-400',   bar:'bg-slate-300' },
    },

    typeMeta: {
      transfer: { label:'Transfer', icon:'ti-arrow-narrow-right', pill:'bg-slate-100 text-slate-600 ring-slate-500/15' },
      hourly:   { label:'Hourly',   icon:'ti-clock-hour-4',       pill:'bg-indigo-50 text-indigo-700 ring-indigo-600/15' },
    },

    statusOrder: { New:0, Quoted:1, Booked:2, Lost:3 },

    payMeta: {
      unsent:    { label:'No deposit yet',     cls:'bg-slate-100 text-slate-500 ring-slate-400/15', icon:'ti-credit-card' },
      requested: { label:'Deposit requested',  cls:'bg-amber-50 text-amber-700 ring-amber-600/15',  icon:'ti-clock' },
      paid:      { label:'Deposit paid',       cls:'bg-emerald-50 text-emerald-700 ring-emerald-600/15', icon:'ti-cash' },
    },
    balMeta: {
      na:        { label:'—',                  cls:'bg-slate-100 text-slate-400 ring-slate-400/15', icon:'ti-minus' },
      scheduled: { label:'Scheduled',          cls:'bg-sky-50 text-sky-700 ring-sky-600/15',        icon:'ti-calendar-clock' },
      paid:      { label:'Paid in full',       cls:'bg-emerald-50 text-emerald-700 ring-emerald-600/15', icon:'ti-circle-check' },
      failed:    { label:'Charge failed',      cls:'bg-rose-50 text-rose-700 ring-rose-600/20',     icon:'ti-credit-card-off' },
    },

    /* Trip / reservation status — exact LimoAnywhere taxonomy, grouped by dispatch phase.
       Mirrored from LimoAnywhere via the status-writeback webhook; editable in-portal for
       off-LA affiliate trips. */
    tripStatusGroups: [
      { phase:'Created',           dot:'bg-slate-400',   cls:'bg-slate-100 text-slate-600 ring-slate-400/20',  statuses:['Unassigned','Farm-out Unassigned','Pending'] },
      { phase:'Offered to Driver', dot:'bg-sky-500',     cls:'bg-sky-50 text-sky-700 ring-sky-600/15',         statuses:['Offered'] },
      { phase:'Driver is Assigned',dot:'bg-indigo-500',  cls:'bg-indigo-50 text-indigo-700 ring-indigo-600/15',statuses:['Assigned','Dispatched - Driver Assigned'] },
      { phase:'En Route to Pickup',dot:'bg-blue-500',    cls:'bg-blue-50 text-blue-700 ring-blue-600/15',      statuses:['On The Way'] },
      { phase:'Circling',          dot:'bg-amber-500',   cls:'bg-amber-50 text-amber-700 ring-amber-600/15',   statuses:['Circling'] },
      { phase:'Waiting at Pickup', dot:'bg-amber-500',   cls:'bg-amber-50 text-amber-800 ring-amber-600/20',   statuses:['Arrived'] },
      { phase:'Driving Passenger', dot:'bg-emerald-500', cls:'bg-emerald-50 text-emerald-700 ring-emerald-600/15', statuses:['Customer In Car'] },
      { phase:'Completing',        dot:'bg-green-600',   cls:'bg-green-50 text-green-700 ring-green-600/20',   statuses:['Done'] },
      { phase:'Cancelled',         dot:'bg-rose-500',    cls:'bg-rose-50 text-rose-700 ring-rose-600/15',      statuses:['Cancelled','Cancelled by Affiliate','Late Cancel','No Show','COVID-19 Cancellation'] },
      { phase:'Offered to Affiliate',dot:'bg-violet-500',cls:'bg-violet-50 text-violet-700 ring-violet-600/15',statuses:['Offered to Affiliate'] },
      { phase:'Affiliate Assigned',dot:'bg-violet-500',  cls:'bg-violet-50 text-violet-700 ring-violet-600/15',statuses:['Affiliate is Assigned'] },
      { phase:'Other',             dot:'bg-slate-400',   cls:'bg-slate-100 text-slate-600 ring-slate-400/20',  statuses:['Dispatched - Driver Assigned NON LA'] },
    ],

    /* =================================================================== */
    /*  Sample leads / quotes                                              */
    /* =================================================================== */
    leads: [
      {
        id: 1, name: 'Sarah Reyes', company: '', channel: 'Wedding Pro', status: 'Quoted',
        phone: '(703) 555-0148', email: 'sarah.reyes@email.com', agent: 'Moe A.',
        created: 'Jun 8', updated: '2m', seq: 980,
        notes: 'Hotel → photos → venue, then a late return after the reception. Wants the coach to stay between ceremony and photos.',
        reservations: [
          { id:'r1a', tripType:'hourly', service:'Wedding — as-directed', date:'Jun 14', time:'3:00 PM',
            vehicle:'Mini Coach (28)', pax:30, hours:6, hourlyRate:295, minHours:4,
            stops:[
              { address:'The Ritz-Carlton, Tysons Corner, VA' },
              { address:'Meadowlark Botanical Gardens, Vienna, VA', note:'30-min photo stop' },
              { address:'Stone Tower Winery, Leesburg, VA' },
            ] },
          { id:'r1b', tripType:'transfer', service:'Wedding — return transfer', date:'Jun 14', time:'10:30 PM',
            vehicle:'Mini Coach (28)', pax:30, baseRate:900,
            stops:[
              { address:'Stone Tower Winery, Leesburg, VA' },
              { address:'The Ritz-Carlton, Tysons Corner, VA' },
            ] },
        ],
        conversation: [
          { out:false, text:'Hi! Looking for transport for our June 14 wedding — about 30 guests, hotel to the venue with a photo stop, and back at the end of the night.', time:'9:02 AM' },
          { out:true,  text:'Congratulations! A 28-passenger mini coach is perfect for that. I’ll keep the coach with you as-directed through photos, then a return at the end — putting the full quote together now.', time:'9:14 AM' },
        ],
        activity: [
          { icon:'ti-heart', label:'Lead captured via Wedding Pro', time:'Jun 8 · 9:02 AM', kind:'rose' },
          { icon:'ti-message-2', label:'Auto-greeting sent via Podium', time:'Jun 8 · 9:02 AM', kind:'muted' },
          { icon:'ti-pencil', label:'Quote drafted — 2 reservations', time:'Jun 8 · 9:20 AM', kind:'muted' },
        ],
      },

      {
        id: 2, name: 'James Tran', company: '', channel: 'Website', status: 'New',
        phone: '(571) 555-0199', email: 'james.tran@email.com', agent: 'Unassigned',
        created: 'Jun 9', updated: '12m', seq: 1010,
        notes: 'Early flight — needs an on-time guarantee. Three large checked bags.',
        reservations: [
          { id:'r2a', tripType:'transfer', service:'Airport transfer — to IAD', date:'Jun 13', time:'6:00 AM',
            vehicle:'Luxury SUV', pax:3, baseRate:185,
            stops:[
              { address:'2100 Crystal Dr, Arlington, VA' },
              { address:'Washington Dulles International (IAD)' },
            ] },
        ],
        conversation: [
          { out:false, text:'Need an SUV to Dulles this Friday at 6am, 3 passengers with luggage.', time:'8:41 AM' },
          { out:true,  text:'Absolutely — a Luxury SUV fits 3 passengers plus bags comfortably. Want me to send a quick quote?', time:'8:46 AM' },
        ],
        activity: [
          { icon:'ti-world', label:'Lead captured via website quote form', time:'Jun 9 · 8:41 AM', kind:'sky' },
          { icon:'ti-message-2', label:'Auto-greeting sent via Podium', time:'Jun 9 · 8:41 AM', kind:'muted' },
        ],
      },

      {
        id: 3, name: 'Denise Walker', company: 'Beltway Capital', channel: 'Phone', status: 'Quoted',
        phone: '(202) 555-0133', email: 'dwalker@beltwaycap.com', agent: 'Moe A.',
        created: 'Jun 7', updated: '18m', seq: 940,
        notes: '3-day investor conference. Arrival + departure shuttles. Invoice to accounts payable — net 30.',
        reservations: [
          { id:'r3a', tripType:'transfer', service:'Conference — arrival shuttle', date:'Jun 20', time:'7:30 AM',
            vehicle:'2× Motor Coach (55)', pax:90, baseRate:4200,
            stops:[
              { address:'Reagan National Airport (DCA)' },
              { address:'Marriott Marquis, Washington, DC' },
            ] },
          { id:'r3b', tripType:'transfer', service:'Conference — departure shuttle', date:'Jun 22', time:'2:00 PM',
            vehicle:'2× Motor Coach (55)', pax:90, baseRate:3900,
            stops:[
              { address:'Marriott Marquis, Washington, DC' },
              { address:'Reagan National Airport (DCA)' },
            ] },
        ],
        conversation: [
          { out:false, text:'We need corporate shuttle service for a 3-day conference, roughly 90 people arriving and departing together.', time:'Yesterday' },
          { out:true,  text:'Happy to help — two 55-passenger motor coaches will cover 90 guests per run. Sending arrival + departure options now.', time:'Yesterday' },
        ],
        activity: [
          { icon:'ti-phone', label:'Phone lead logged via quick intake', time:'Jun 7 · 4:10 PM', kind:'emerald' },
          { icon:'ti-pencil', label:'Quote drafted — 2 reservations', time:'Jun 7 · 4:25 PM', kind:'muted' },
          { icon:'ti-send', label:'Quote sent via Podium', time:'Jun 7 · 4:31 PM', kind:'gold' },
        ],
      },

      {
        id: 4, name: 'Olivia Grant', company: '', channel: 'Wedding Pro', status: 'Booked',
        phone: '(240) 555-0177', email: 'olivia.g@email.com', agent: 'Moe A.',
        created: 'Jun 5', updated: '2h', seq: 900, accountId: 'LA-ACC-2204',
        notes: 'Champagne service requested. Deposit paid. Photo stop at the Tidal Basin on the way to the estate.',
        reservations: [
          { id:'r4a', tripType:'hourly', service:'Wedding — as-directed', date:'Jul 2', time:'4:00 PM',
            vehicle:'Stretch Limousine', pax:10, hours:5, hourlyRate:240, minHours:4, laResId:'LA-5012',
            stops:[
              { address:'The Mayflower Hotel, Washington, DC' },
              { address:'Tidal Basin, Washington, DC', note:'20-min photo stop' },
              { address:'Private Estate, Potomac, MD' },
            ] },
        ],
        conversation: [
          { out:false, text:'Do you have a stretch limo for July 2, wedding party of 10? We’d love a quick photo stop on the way.', time:'Jun 5' },
          { out:true,  text:'We do! I’ve booked the stretch as-directed for 5 hours with your Tidal Basin photo stop. You’re all set — confirmation on the way.', time:'Jun 5' },
        ],
        activity: [
          { icon:'ti-heart', label:'Lead captured via Wedding Pro', time:'Jun 5 · 11:20 AM', kind:'rose' },
          { icon:'ti-send', label:'Quote sent via Podium', time:'Jun 5 · 12:02 PM', kind:'gold' },
          { icon:'ti-circle-check', label:'Booked — 1 reservation created in LimoAnywhere', time:'Jun 5 · 2:15 PM', kind:'green' },
        ],
      },

      {
        id: 5, name: 'Marcus Kelly', company: '', channel: 'Website', status: 'New',
        phone: '(301) 555-0162', email: 'mkelly@email.com', agent: 'Unassigned',
        created: 'Jun 9', updated: '1h', seq: 1005,
        notes: 'Group of 8 with luggage heading to DCA. Flexible on pickup time by ~15 min.',
        reservations: [
          { id:'r5a', tripType:'transfer', service:'Airport transfer — to DCA', date:'Jun 17', time:'9:00 AM',
            vehicle:'Sprinter Van (14)', pax:8, baseRate:240,
            stops:[
              { address:'7700 Old Georgetown Rd, Bethesda, MD' },
              { address:'Reagan National Airport (DCA)' },
            ] },
        ],
        conversation: [
          { out:false, text:'Airport pickup for 8 next Tuesday morning? We have a few bags.', time:'7:55 AM' },
        ],
        activity: [
          { icon:'ti-world', label:'Lead captured via website quote form', time:'Jun 9 · 7:55 AM', kind:'sky' },
        ],
      },

      {
        id: 6, name: 'Priya Anand', company: '', channel: 'Website', status: 'Lost',
        phone: '(703) 555-0121', email: 'priya.anand@email.com', agent: 'Moe A.',
        created: 'Jun 4', updated: '5d', seq: 860, lostReason: 'Price — booked a lower-cost sedan elsewhere.',
        notes: 'Single airport run. Compared three vendors.',
        reservations: [
          { id:'r6a', tripType:'transfer', service:'Airport transfer — to IAD', date:'May 30', time:'5:00 AM',
            vehicle:'Luxury Sedan', pax:2, baseRate:145,
            stops:[
              { address:'1450 Chain Bridge Rd, McLean, VA' },
              { address:'Washington Dulles International (IAD)' },
            ] },
        ],
        conversation: [
          { out:false, text:'Quote for a sedan to Dulles on the 30th, very early?', time:'Jun 4' },
          { out:true,  text:'Sure — sedan to IAD at 5am is $145. Shall I lock it in?', time:'Jun 4' },
          { out:false, text:'Thanks, going to go another direction this time.', time:'Jun 4' },
        ],
        activity: [
          { icon:'ti-world', label:'Lead captured via website quote form', time:'Jun 4', kind:'sky' },
          { icon:'ti-circle-x', label:'Marked lost — price', time:'Jun 4', kind:'muted' },
        ],
      },
    ],

    /* =================================================================== */
    /*  Contacts directory                                                 */
    /* =================================================================== */
    contacts: [
      { name:'Sarah Reyes', company:'', channel:'Wedding Pro', phone:'(703) 555-0148', email:'sarah.reyes@email.com', ltv:2670, trips:2, last:'2m ago', leadId:1 },
      { name:'Denise Walker', company:'Beltway Capital', channel:'Phone', phone:'(202) 555-0133', email:'dwalker@beltwaycap.com', ltv:8100, trips:2, last:'18m ago', leadId:3 },
      { name:'Olivia Grant', company:'', channel:'Wedding Pro', phone:'(240) 555-0177', email:'olivia.g@email.com', ltv:1200, trips:1, last:'2h ago', leadId:4 },
      { name:'James Tran', company:'', channel:'Website', phone:'(571) 555-0199', email:'james.tran@email.com', ltv:185, trips:1, last:'12m ago', leadId:2 },
      { name:'Marcus Kelly', company:'', channel:'Website', phone:'(301) 555-0162', email:'mkelly@email.com', ltv:240, trips:1, last:'1h ago', leadId:5 },
      { name:'Robert Maddox', company:'Maddox & Vale LLP', channel:'Phone', phone:'(202) 555-0190', email:'rmaddox@mvlaw.com', ltv:14250, trips:9, last:'3w ago', leadId:null },
      { name:'Elena Cruz', company:'', channel:'Wedding Pro', phone:'(571) 555-0144', email:'elena.cruz@email.com', ltv:3400, trips:2, last:'1mo ago', leadId:null },
      { name:'TechSummit Events', company:'TechSummit', channel:'API', phone:'(800) 555-0107', email:'logistics@techsummit.io', ltv:21600, trips:18, last:'6w ago', leadId:null },
    ],

    /* =================================================================== */
    /*  Reviews (Podium review invites + ratings)                          */
    /* =================================================================== */
    reviews: [
      { name:'Olivia Grant', trip:'Wedding — as-directed', status:'Completed', rating:5, when:'Jun 6', text:'Driver was early, immaculate limo, the photo stop made our day. Flawless.' },
      { name:'Robert Maddox', trip:'Corporate transfer', status:'Completed', rating:5, when:'May 28', text:'On time every single run for our roadshow. Our go-to now.' },
      { name:'Elena Cruz', trip:'Wedding — return transfer', status:'Completed', rating:4, when:'May 12', text:'Great service, slight delay on pickup but communication was excellent.' },
      { name:'Denise Walker', trip:'Conference — arrival shuttle', status:'Pending', rating:0, when:'Scheduled Jun 22', text:'' },
      { name:'James Tran', trip:'Airport transfer — to IAD', status:'Pending', rating:0, when:'Scheduled Jun 13', text:'' },
    ],

    reviewStats() {
      const done = this.reviews.filter(r=>r.status==='Completed');
      const avg = done.length ? (done.reduce((s,r)=>s+r.rating,0)/done.length) : 0;
      return { avg: avg.toFixed(1), count: done.length, pending: this.reviews.filter(r=>r.status==='Pending').length };
    },

    /* =================================================================== */
    /*  Lifecycle                                                          */
    /* =================================================================== */
    init() {
      this.navOpen = window.innerWidth >= 1024;
      this.selectedId = this.leads[0].id;
      this.convoId = this.leads[0].id;
      const h = (location.hash || '').replace('#', '');
      if (['inbox','leads','pipeline','contacts','reviews','settings','workspace'].includes(h)) this.view = h;
      this.$watch('view', v => { try { history.replaceState(null, '', v === 'leads' ? location.pathname : '#' + v); } catch (e) {} });

      // payments — seed a default state per lead, then a failed balance for the demo
      this.leads.forEach(l => {
        if (l.payment) return;
        if (l.status === 'Booked')      l.payment = { depositStatus:'paid',      balanceStatus:'scheduled', card:{ brand:'Visa', last4:'4242' }, failReason:'' };
        else if (l.status === 'Quoted') l.payment = { depositStatus:'requested', balanceStatus:'na',        card:null,                          failReason:'' };
        else                            l.payment = { depositStatus:'unsent',    balanceStatus:'na',        card:null,                          failReason:'' };
      });
      const failed = this.leads.find(l => l.id === 4); // Olivia — booked, balance declined 30 days out
      if (failed) {
        failed.payment.balanceStatus = 'failed';
        failed.payment.failReason = 'Card declined — insufficient funds';
        failed.alert = true;
        failed.activity.unshift({ icon:'ti-credit-card-off', label:'Balance charge failed — card declined', time:'Jun 2', kind:'muted' });
        this.pushNotif(failed, 'balance_failed');
        failed.reservations.forEach(r => r.tripStatus = 'Assigned'); // mirrored from LimoAnywhere
      }
    },

    /* =================================================================== */
    /*  Navigation                                                         */
    /* =================================================================== */
    go(v) { this.view = v; this.navOpen = window.innerWidth >= 1024 ? this.navOpen : false; },
    openLead(l) { this.selectedId = l.id; this.view = 'workspace'; },
    openLeadById(id) { this.selectedId = id; this.view = 'workspace'; },
    newLead() {
      const id = Math.max(0, ...this.leads.map(l => l.id)) + 1;
      const r = this.blankReservation();
      r.service = 'New reservation';
      const l = {
        id, name: 'New lead', company: '', channel: 'Website', status: 'New',
        phone: '', email: '', agent: 'Unassigned', created: 'today', updated: 'now', seq: 9999,
        notes: '', reservations: [r], conversation: [],
        activity: [{ icon:'ti-plus', label:'Lead created manually', time:'now', kind:'muted' }],
      };
      this.leads.unshift(l);
      this.selectedId = id; this.view = 'workspace';
      this.toast('New lead created — add details');
    },

    get selected() { return this.leads.find(l => l.id === this.selectedId) || this.leads[0]; },
    get convo()    { return this.leads.find(l => l.id === this.convoId) || this.leads[0]; },

    /* =================================================================== */
    /*  Pricing + derived figures                                          */
    /* =================================================================== */
    billedHours(r) { return Math.max(r.hours || 0, r.minHours || 0); },
    minApplied(r)  { return r.tripType === 'hourly' && (r.hours || 0) < (r.minHours || 0); },
    surTotal(r)    { return (r.surcharges || []).reduce((s,x) => s + (x.amount || 0), 0); },
    resTotal(r) {
      const base = r.tripType === 'hourly' ? this.billedHours(r) * (r.hourlyRate || 0) : (r.baseRate || 0);
      return base + this.surTotal(r);
    },
    quoteTotal(l) { return (l.reservations || []).reduce((s,r) => s + this.resTotal(r), 0); },

    /* payments */
    depositAmount(l) { return Math.round(this.quoteTotal(l) * this.depositPct / 100); },
    balanceAmount(l) { return this.quoteTotal(l) - this.depositAmount(l); },
    earliestPickup(l) {
      const ds = (l.reservations || []).map(r => new Date(r.date + ' 2026')).filter(d => !isNaN(d.getTime()));
      return ds.length ? new Date(Math.min(...ds.map(d => d.getTime()))) : null;
    },
    balanceDueDate(l) { const p = this.earliestPickup(l); if (!p) return null; const d = new Date(p.getTime()); d.setDate(d.getDate() - 30); return d; },
    balanceDueLabel(l) { const d = this.balanceDueDate(l); return d ? d.toLocaleDateString('en-US', { month:'short', day:'numeric' }) : '—'; },
    balanceDueNow(l) { const d = this.balanceDueDate(l); return d ? d.getTime() <= Date.now() : false; },
    payChip(l) {
      const p = l.payment; if (!p) return null;
      if (p.balanceStatus === 'failed')    return { label:'Balance failed',            cls:'bg-rose-50 text-rose-700 ring-rose-600/20',      icon:'ti-credit-card-off' };
      if (p.balanceStatus === 'paid')      return { label:'Paid in full',             cls:'bg-emerald-50 text-emerald-700 ring-emerald-600/15', icon:'ti-circle-check' };
      if (p.balanceStatus === 'scheduled') return { label:'Bal · ' + this.balanceDueLabel(l), cls:'bg-sky-50 text-sky-700 ring-sky-600/15',   icon:'ti-calendar-clock' };
      if (p.depositStatus === 'paid')      return { label:'Deposit paid',             cls:'bg-emerald-50 text-emerald-700 ring-emerald-600/15', icon:'ti-cash' };
      if (p.depositStatus === 'requested') return { label:'Deposit requested',        cls:'bg-amber-50 text-amber-700 ring-amber-600/15',   icon:'ti-clock' };
      return null;
    },

    /* trip / reservation status */
    tripMeta(status) {
      for (const g of this.tripStatusGroups) if (g.statuses.includes(status)) return g;
      return { phase:'', dot:'bg-slate-400', cls:'bg-slate-100 text-slate-600 ring-slate-400/20' };
    },
    tripCancelled(status) { return this.tripMeta(status).phase === 'Cancelled'; },
    setTripStatus(res, s) {
      res.tripStatus = s;
      const l = this.selected;
      l.activity.unshift({ icon:'ti-route', label:`Trip “${res.service}” → ${s}`, time:'now', kind:'muted' });
      if (s === 'Done') {
        l.activity.unshift({ icon:'ti-star', label:'Trip completed — review request scheduled via Podium', time:'now', kind:'gold' });
        this.toast('Trip completed — review request scheduled', 'gold');
      } else if (this.tripCancelled(s)) {
        this.toast('Trip · ' + s, 'amber');
      } else {
        this.toast('Trip status → ' + s);
      }
    },

    /* stops / routing */
    pickup(r)      { return r.stops[0]?.address || ''; },
    dropoff(r)     { return r.stops[r.stops.length - 1]?.address || ''; },
    midStops(r)    { return r.stops.slice(1, -1); },
    isMultiStop(r) { return r.stops.length > 2; },
    shortAddr(a)   { return (a || '').split(',')[0].trim(); },

    /* =================================================================== */
    /*  Formatting + classes                                               */
    /* =================================================================== */
    money(n)    { return '$' + (Math.round(n || 0)).toLocaleString('en-US'); },
    money0(n)   { return (Math.round(n || 0)).toLocaleString('en-US'); },
    quoteNo(l)  { return 'Q-' + (1040 + l.id); },
    initials(name) { return (name || '').split(' ').filter(Boolean).map(w => w[0]).slice(0,2).join('').toUpperCase(); },
    ch(l)       { return this.channelMeta[l.channel] || this.channelMeta['Website']; },
    st(l)       { return this.statusMeta[l.status] || this.statusMeta['New']; },
    ty(r)       { return this.typeMeta[r.tripType] || this.typeMeta['transfer']; },
    stepState(i){ const c = this.statusOrder[this.selected.status]; return c > i ? 'done' : c === i ? 'current' : 'todo'; },

    /* =================================================================== */
    /*  Leads list — filter / sort / counts                                */
    /* =================================================================== */
    setSort(k) { if (this.sortKey === k) { this.sortDir *= -1; } else { this.sortKey = k; this.sortDir = (k === 'name') ? 1 : -1; } },

    get filtered() {
      const ql = this.q.trim().toLowerCase();
      let arr = this.leads.filter(l => {
        if (this.filter !== 'All' && l.status !== this.filter) return false;
        if (this.channelFilter !== 'All' && l.channel !== this.channelFilter) return false;
        if (ql) {
          const hay = (l.name + ' ' + l.company + ' ' + l.channel + ' ' +
            l.reservations.map(r => r.service + ' ' + r.stops.map(s => s.address).join(' ')).join(' ')).toLowerCase();
          if (!hay.includes(ql)) return false;
        }
        return true;
      });
      const dir = this.sortDir;
      arr.sort((a,b) => {
        let av, bv;
        switch (this.sortKey) {
          case 'name':   av = a.name.toLowerCase(); bv = b.name.toLowerCase(); break;
          case 'total':  av = this.quoteTotal(a);   bv = this.quoteTotal(b);   break;
          case 'status': av = this.statusOrder[a.status]; bv = this.statusOrder[b.status]; break;
          case 'trips':  av = a.reservations.length; bv = b.reservations.length; break;
          default:       av = a.seq; bv = b.seq;  // recent
        }
        return av < bv ? -dir : av > bv ? dir : 0;
      });
      return arr;
    },

    get counts() {
      const by = s => this.leads.filter(l => l.status === s).length;
      return { New: by('New'), Quoted: by('Quoted'), Booked: by('Booked'), Lost: by('Lost') };
    },
    get openPipeline() {
      return this.leads.filter(l => l.status === 'New' || l.status === 'Quoted')
        .reduce((s,l) => s + this.quoteTotal(l), 0);
    },
    get inboxCount() { return this.leads.filter(l => l.status === 'New' || l.status === 'Quoted').length; },

    /* =================================================================== */
    /*  Reservation editor                                                 */
    /* =================================================================== */
    blankReservation() {
      return {
        id: 'r' + Date.now(), tripType: 'transfer', service: '', date: '', time: '',
        vehicle: 'Luxury SUV', pax: 1, baseRate: 0, hours: 4, hourlyRate: 295, minHours: 4,
        stops: [ { address:'' }, { address:'' } ],
      };
    },
    newReservation() { this.draft = this.blankReservation(); this.draftIsNew = true; this.editorOpen = true; },
    editReservation(r) { this.draft = JSON.parse(JSON.stringify(r)); this.draftIsNew = false; this.editorOpen = true; },
    closeEditor() { this.editorOpen = false; this.draft = null; },

    setDraftType(t) {
      this.draft.tripType = t;
      if (t === 'hourly') { this.draft.hours = this.draft.hours || 4; this.draft.hourlyRate = this.draft.hourlyRate || 295; this.draft.minHours = this.draft.minHours || 4; }
      else { this.draft.baseRate = this.draft.baseRate || 0; }
    },
    addStop() { this.draft.stops.splice(this.draft.stops.length - 1, 0, { address:'' }); },
    removeStop(i) { if (this.draft.stops.length > 2) this.draft.stops.splice(i, 1); },
    stopLabel(i, len) { return i === 0 ? 'Pickup' : i === len - 1 ? 'Drop-off' : 'Stop ' + i; },

    saveReservation() {
      const l = this.selected;
      const d = this.draft;
      if (!d.service) d.service = d.tripType === 'hourly' ? 'As-directed charter' : 'Transfer';
      d.pax = Number(d.pax) || 1;
      d.baseRate = Number(d.baseRate) || 0;
      d.hours = Number(d.hours) || 0; d.hourlyRate = Number(d.hourlyRate) || 0; d.minHours = Number(d.minHours) || 0;
      if (this.draftIsNew) { l.reservations.push(d); this.toast('Reservation added to quote'); }
      else {
        const i = l.reservations.findIndex(r => r.id === d.id);
        if (i > -1) l.reservations.splice(i, 1, d);
        this.toast('Reservation updated');
      }
      if (l.status === 'Booked') { l.status = 'Quoted'; this.toast('Quote re-opened — re-book to re-sync LimoAnywhere', 'amber'); }
      this.closeEditor();
    },
    duplicateReservation(r) {
      const c = JSON.parse(JSON.stringify(r));
      c.id = 'r' + Date.now(); c.service = (c.service || 'Reservation') + ' (copy)'; c.laResId = null;
      this.selected.reservations.push(c); this.toast('Reservation duplicated');
    },
    removeReservation(r) {
      const l = this.selected;
      if (l.reservations.length <= 1) { this.toast('A quote needs at least one reservation', 'amber'); return; }
      l.reservations = l.reservations.filter(x => x.id !== r.id);
      this.toast('Reservation removed');
    },

    /* =================================================================== */
    /*  Quote actions                                                      */
    /* =================================================================== */
    sendQuote(l) {
      l.status = 'Quoted';
      if (!l.payment) l.payment = { depositStatus:'unsent', balanceStatus:'na', card:null, failReason:'' };
      l.payment.depositStatus = 'requested';
      l.conversation.push({ out:true, time:'now',
        text:`Here’s your quote, ${l.name.split(' ')[0]} — ${this.money(this.quoteTotal(l))} total. To lock in your date, a ${this.depositPct}% deposit (${this.money(this.depositAmount(l))}) secures the booking; the balance is charged 30 days before pickup. Pay securely: pay.allprocharter.com/${this.quoteNo(l)}` });
      l.activity.unshift({ icon:'ti-send', label:`Quote + ${this.depositPct}% deposit request sent via Podium`, time:'now', kind:'gold' });
      this.toast('Quote + deposit request sent', 'gold');
    },
    markDepositPaid(l) {
      if (!l.payment) l.payment = { depositStatus:'unsent', balanceStatus:'na', card:null, failReason:'' };
      l.payment.depositStatus = 'paid';
      l.payment.card = { brand:'Visa', last4:'4242' };
      l.activity.unshift({ icon:'ti-cash', label:`Deposit paid — ${this.money(this.depositAmount(l))} · card on file ••••4242`, time:'now', kind:'green' });
      this.pushNotif(l, 'deposit_paid');
      this.markBooked(l); // auto-book: sync to LimoAnywhere + schedule the balance
    },
    resendQuote(l) {
      l.conversation.push({ out:true, time:'now', text:'Resending your quote — happy to adjust anything.' });
      this.toast('Quote resent via Podium', 'gold');
    },
    markLost(l) {
      l.status = 'Lost'; l.lostReason = l.lostReason || 'Marked lost';
      l.activity.unshift({ icon:'ti-circle-x', label:'Marked lost', time:'now', kind:'muted' });
      this.toast('Lead marked lost');
    },

    /* ----------------------------------------- balance charge (30 days out) */
    simulateBalance(l, outcome) {
      if (outcome === 'success') {
        l.payment.balanceStatus = 'paid'; l.payment.failReason = ''; l.alert = false;
        l.activity.unshift({ icon:'ti-circle-check', label:`Balance charged — ${this.money(this.balanceAmount(l))} · paid in full`, time:'now', kind:'green' });
        this.pushNotif(l, 'balance_paid');
        this.toast('Balance charged — paid in full', 'green');
      } else {
        l.payment.balanceStatus = 'failed'; l.payment.failReason = 'Card declined — insufficient funds'; l.alert = true;
        l.activity.unshift({ icon:'ti-credit-card-off', label:'Balance charge failed — card declined', time:'now', kind:'muted' });
        this.pushNotif(l, 'balance_failed');
        this.toast('Balance charge failed — card declined', 'amber');
      }
    },
    retryBalance(l) {
      l.payment.balanceStatus = 'paid'; l.payment.failReason = ''; l.alert = false;
      l.activity.unshift({ icon:'ti-refresh', label:`Balance retried — ${this.money(this.balanceAmount(l))} · paid in full`, time:'now', kind:'green' });
      this.notifications.filter(n => n.leadId === l.id && n.kind === 'balance_failed').forEach(n => n.read = true);
      this.pushNotif(l, 'balance_paid');
      this.toast('Balance retried — paid in full', 'green');
    },
    requestNewCard(l) {
      l.conversation.push({ out:true, time:'now', text:`Hi ${l.name.split(' ')[0]} — we couldn’t process your balance payment. Could you update your card here? pay.allprocharter.com/update/${this.quoteNo(l)}` });
      l.activity.unshift({ icon:'ti-message-2', label:'Requested updated card via Podium', time:'now', kind:'gold' });
      this.toast('New-card request sent via Podium', 'gold');
    },

    /* ----------------------------------------------------- notifications */
    pushNotif(lead, kind) {
      const id = ++this.notifSeq;
      const map = {
        balance_failed: { title:'Balance charge failed', icon:'ti-credit-card-off', detail:`${lead.name} · ${this.money(this.balanceAmount(lead))} · ${lead.payment.failReason}` },
        balance_paid:   { title:'Balance paid in full',  icon:'ti-circle-check',    detail:`${lead.name} · ${this.money(this.balanceAmount(lead))}` },
        deposit_paid:   { title:'Deposit received',      icon:'ti-cash',            detail:`${lead.name} · ${this.money(this.depositAmount(lead))} · auto-booked` },
      };
      const m = map[kind] || { title:'Update', icon:'ti-bell', detail:lead.name };
      this.notifications.unshift({ id, leadId:lead.id, kind, title:m.title, icon:m.icon, detail:m.detail, time:'now', read:false });
    },
    get unreadNotifs() { return this.notifications.filter(n => !n.read).length; },
    openNotif(n) { n.read = true; this.notifOpen = false; this.openLeadById(n.leadId); },
    markAllNotifsRead() { this.notifications.forEach(n => n.read = true); },

    /* =================================================================== */
    /*  Booking → LimoAnywhere sync animation                              */
    /* =================================================================== */
    markBooked(l) {
      this.syncLeadId = l.id; this.syncDone = false; this.syncOpen = true;
      this.syncSteps = [
        { label:'Find / Create Account', sub:l.name + (l.company ? ' · ' + l.company : ''), status:'pending', icon:'ti-user-plus' },
        { label:'Create Quote Request', sub:this.quoteNo(l) + ' · ' + this.money(this.quoteTotal(l)), status:'pending', icon:'ti-file-invoice' },
        ...l.reservations.map(r => ({ label:'Create Reservation', sub:r.service + ' · ' + r.date, status:'pending', icon:'ti-calendar-plus', resId:r.id })),
        { label:'Schedule balance charge', sub:this.money(this.balanceAmount(l)) + ' · due ' + this.balanceDueLabel(l), status:'pending', icon:'ti-calendar-clock', schedule:true },
      ];
      this.runSync(l, 0);
    },
    runSync(l, i) {
      if (i > 0) this.syncSteps[i - 1].status = 'done';
      if (i < this.syncSteps.length) {
        this.syncSteps[i].status = 'running';
        const s = this.syncSteps[i];
        setTimeout(() => {
          if (s.resId) {
            const r = l.reservations.find(x => x.id === s.resId);
            if (r) { r.laResId = 'LA-' + (this.laCounter++); s.sub = s.sub + '  →  ' + r.laResId; }
          }
          this.runSync(l, i + 1);
        }, 760);
      } else {
        l.status = 'Booked';
        l.accountId = l.accountId || ('LA-ACC-' + (2200 + l.id));
        l.reservations.forEach(r => { if (!r.tripStatus) r.tripStatus = 'Unassigned'; }); // trip enters LA dispatch queue
        if (l.payment) { l.payment.balanceStatus = 'scheduled'; if (!l.payment.card) l.payment.card = { brand:'Visa', last4:'4242' }; }
        l.activity.unshift({ icon:'ti-circle-check', label:`Booked — ${l.reservations.length} reservation${l.reservations.length>1?'s':''} in LimoAnywhere · balance scheduled ${this.balanceDueLabel(l)}`, time:'now', kind:'green' });
        this.syncDone = true;
        this.toast(`Booked — balance scheduled for ${this.balanceDueLabel(l)}`, 'green');
      }
    },
    closeSync() { this.syncOpen = false; this.syncSteps = []; },

    /* =================================================================== */
    /*  Pipeline (kanban)                                                  */
    /* =================================================================== */
    pipelineStages: ['New','Quoted','Booked','Lost'],
    columnLeads(s) { return this.leads.filter(l => l.status === s).sort((a,b)=>b.seq-a.seq); },
    columnValue(s) { return this.columnLeads(s).reduce((sum,l) => sum + this.quoteTotal(l), 0); },
    onDragStart(l) { this.draggingId = l.id; },
    onDragEnd() { this.draggingId = null; this.dragOver = null; },
    onDrop(status) {
      const l = this.leads.find(x => x.id === this.draggingId);
      if (l && l.status !== status) {
        const from = l.status; l.status = status;
        l.activity.unshift({ icon:'ti-arrows-exchange', label:`Moved ${from} → ${status}`, time:'now', kind:'muted' });
        this.toast(`${l.name}: ${from} → ${status}`);
      }
      this.draggingId = null; this.dragOver = null;
    },

    /* =================================================================== */
    /*  Inbox                                                              */
    /* =================================================================== */
    get inboxList() {
      const ql = this.convoQ.trim().toLowerCase();
      return this.leads
        .filter(l => !ql || (l.name + ' ' + l.reservations.map(r=>r.service).join(' ')).toLowerCase().includes(ql))
        .slice().sort((a,b) => b.seq - a.seq);
    },
    lastPreview(l) { const m = l.conversation[l.conversation.length - 1]; return m ? (m.out ? 'You: ' : '') + m.text : ''; },
    selectConvo(l) { this.convoId = l.id; },
    replyTo(lead) {
      const t = this.replyText.trim(); if (!t) return;
      lead.conversation.push({ out:true, text:t, time:'now' });
      this.replyText = '';
      this.toast('Sent via Podium', 'gold');
    },

    /* =================================================================== */
    /*  Toasts                                                             */
    /* =================================================================== */
    toast(msg, kind = 'default') {
      const id = ++this.toastSeq;
      this.toasts.push({ id, msg, kind });
      setTimeout(() => { this.toasts = this.toasts.filter(t => t.id !== id); }, 3400);
    },
  };
}
