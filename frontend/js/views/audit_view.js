export const auditViewTemplate = `
  <div v-show="tab===5" class="panel">
    <h2>审计与争用</h2>
    <h3>当事人互证</h3>
    <div>
      <input v-model="proofKey" placeholder="当事人 party_key" />
      <button class="ghost" @click="loadProof">互证查询</button>
    </div>
    <p v-if="proof" :class="proof.hold_drift ? 'err' : 'ok'">
      {{ proof.party_key }}：占位 {{ proof.holds.length }} 条 / 正式册 {{ proof.bookings.length }} 条 / 候补 {{ proof.waitlist.length }} 条
      <template v-if="proof.hold_drift"> —— 同档双占位偏航（档 ID {{ proof.duplicate_slot_ids.join('、') }}）</template>
      <template v-else> —— 同档占位唯一</template>
    </p>
    <h3>争用</h3>
    <table>
      <thead><tr><th>锁</th><th>人</th><th>动作</th><th>详情</th></tr></thead>
      <tbody>
        <tr v-for="c in contention" :key="c.id">
          <td>{{ c.lock_key }}</td><td>{{ c.actor }}</td><td>{{ c.action }}</td><td>{{ c.detail }}</td>
        </tr>
      </tbody>
    </table>
    <h3>审计</h3>
    <table>
      <thead><tr><th>人</th><th>动作</th><th>目标</th><th>时</th></tr></thead>
      <tbody>
        <tr v-for="a in audits" :key="a.id"
            :class="(a.action==='hold_idempotent_reject'||a.action==='hold_idempotency_drift')?'err':''">
          <td>{{ a.actor }}</td><td>{{ a.action }}</td><td>{{ a.target }}</td><td>{{ a.created_at }}</td>
        </tr>
      </tbody>
    </table>
  </div>
`;
