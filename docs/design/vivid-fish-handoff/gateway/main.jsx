// main.jsx — compose the design canvas.
// v0.5: scope cuts (no policy simulator, no dedicated receipt v1, no separate
// capabilities/activity tabs) + full desktop adaptations alongside mobile.

const PHONE_W = 400;
const PHONE_H = 800;
const DESK_W  = 1400;
const DESK_H  = 900;

function App() {
  return (
    <DesignCanvas
      title="ai.vivid.fish — UX design v0.5"
      subtitle="13 screens. Mobile is canonical; desktop is the same surfaces with the room they deserve. Activity, capabilities, and the policy simulator were cut."
    >
      <DCSection id="brief" title="00 · Read me first">
        <DCArtboard id="brief" label="The brief · for the dev agent" width={1400} height={2400}><Brief/></DCArtboard>
      </DCSection>

      <DCSection id="user" title="A · USER · mobile · thin on purpose">
        <DCArtboard id="signin"     label="A.1 · Sign in"               width={PHONE_W} height={PHONE_H}><ScreenSignIn/></DCArtboard>
        <DCArtboard id="home-empty" label="A.2 · Home · first time"     width={PHONE_W} height={PHONE_H}><ScreenHomeEmpty/></DCArtboard>
        <DCArtboard id="home-active"label="A.3 · Home · returning"      width={PHONE_W} height={PHONE_H}><ScreenHomeActive/></DCArtboard>
        <DCArtboard id="create-1"   label="A.4 · New key · name (1/2)"  width={PHONE_W} height={PHONE_H}><ScreenCreate1/></DCArtboard>
        <DCArtboard id="create-3"   label="A.5 · New key · reveal (2/2)"width={PHONE_W} height={PHONE_H}><ScreenCreate3/></DCArtboard>
        <DCArtboard id="key-detail" label="A.6 · App / key detail"      width={PHONE_W} height={PHONE_H}><ScreenKeyDetail/></DCArtboard>
        <DCArtboard id="receipt-v2" label="A.7 · Receipt detail"        width={PHONE_W} height={PHONE_H}><ScreenReceiptDetailV2/></DCArtboard>
      </DCSection>

      <DCSection id="admin" title="B · ADMIN · mobile · the actual work">
        <DCArtboard id="admin-home"        label="B.1 · Operating overview"            width={PHONE_W} height={PHONE_H}><ScreenAdminHome/></DCArtboard>
        <DCArtboard id="admin-people"      label="B.2 · People"                         width={PHONE_W} height={PHONE_H}><ScreenAdminPeople/></DCArtboard>
        <DCArtboard id="admin-user"        label="B.3 · One person · Ada"               width={PHONE_W} height={PHONE_H}><ScreenAdminUserDetail/></DCArtboard>
        <DCArtboard id="admin-invite"      label="B.4 · Invite someone"                 width={PHONE_W} height={PHONE_H}><ScreenAdminInvite/></DCArtboard>
        <DCArtboard id="admin-services"    label="B.5 · Service apps (admin-owned)"     width={PHONE_W} height={PHONE_H}><ScreenAdminServiceApps/></DCArtboard>
        <DCArtboard id="admin-service-new" label="B.6 · Add a service app"              width={PHONE_W} height={PHONE_H}><ScreenAdminCreateServiceApp/></DCArtboard>
        <DCArtboard id="admin-profiles"    label="B.7 · Profiles list"                  width={PHONE_W} height={PHONE_H}><ScreenAdminProfiles/></DCArtboard>
        <DCArtboard id="admin-profile-tpl" label="B.8 · New profile · pick a template"  width={PHONE_W} height={PHONE_H}><ScreenAdminProfileFromTemplate/></DCArtboard>
        <DCArtboard id="admin-profile"     label="B.9 · Profile editor · centerpiece"   width={PHONE_W} height={PHONE_H}><ScreenAdminProfileEditor/></DCArtboard>
        <DCArtboard id="admin-debug"       label="B.10 · Debug a denial"                width={PHONE_W} height={PHONE_H}><ScreenAdminDebug/></DCArtboard>
      </DCSection>

      <DCSection id="desktop-user" title="C · USER · desktop">
        <DCArtboard id="dt-home"    label="C.1 · Home · desktop"    width={DESK_W} height={DESK_H}><ScreenDesktopUserHome/></DCArtboard>
        <DCArtboard id="dt-receipt" label="C.2 · Receipt · desktop" width={DESK_W} height={DESK_H}><ScreenDesktopReceipt/></DCArtboard>
      </DCSection>

      <DCSection id="desktop-admin" title="D · ADMIN · desktop · the workspace">
        <DCArtboard id="dt-admin-home"   label="D.1 · Operating · desktop"     width={DESK_W} height={DESK_H}><ScreenDesktopAdminHome/></DCArtboard>
        <DCArtboard id="dt-admin-editor" label="D.2 · Profile editor · desktop"width={DESK_W} height={DESK_H}><ScreenDesktopProfileEditor/></DCArtboard>
      </DCSection>
    </DesignCanvas>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(<App/>);
