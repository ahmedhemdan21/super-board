# Hub boards — one board, several repos

A hub board covers several member repos (for example a backend and a frontend that are
submodules of one parent). Reports are filed once as parent issues in a hub repo, triage splits
them into per-repo sub-issues, and one run builds both. Code, PRs, merge gates and deploys stay
in each member repo. Single-repo configs (no `members`) are unchanged.

## What GitHub does (checked 2026-10-19)

Checked on throwaway private repos and a user Project; queries below are the ones used.

| Question | Answer | What super-board does about it |
|---|---|---|
| Sub-issues across repos? | Yes. `addSubIssue(input:{issueId, subIssueId})` attaches an issue from another repo of the same owner; `parent`, `subIssues`, `subIssuesSummary` read back on both sides. | The split attaches each member issue to the hub parent. |
| Is `subIssuesSummary` current right after attaching? | No. It read `total: 1` for two children seconds after attaching; later `2`. | Count children from `subIssues.nodes`, never from the summary. |
| Does the parent close when every child closes? | No. Parent stays `OPEN` at 100 %. | super-board closes the parent itself when every child closed as completed. |
| One Project with items from several repos? | Yes. Three `#1` issues from three repos are three separate items. Built-in fields: **Repository**, **Parent issue**, **Sub-issues progress**. | Cards are `owner/repo#N`; views filter on Repository. |
| Moving a one-sided report to a member repo? | `gh issue transfer` keeps comments and the Project item (it now points at the new issue); the old URL still resolves. Labels are **dropped**, even when the target has the same label. | Re-apply labels after the transfer. |
| Cost of one board read? | 100 items with repo, labels, parent and sub-issues: 2 points (1 without the parent fields). | One hub read costs what two single-repo reads cost today. |

```graphql
mutation($p:ID!,$c:ID!){ addSubIssue(input:{issueId:$p, subIssueId:$c}){ subIssue{ number repository{ nameWithOwner } } } }

query($o:String!,$n:Int!){ rateLimit{ cost }
  user(login:$o){ projectV2(number:$n){ items(first:100){ nodes{ id
    fieldValueByName(name:"Status"){ ...on ProjectV2ItemFieldSingleSelectValue{ name } }
    content{ ...on Issue{ number title state labels(first:20){ nodes{ name } }
      repository{ nameWithOwner } parent{ number repository{ nameWithOwner } }
      subIssues(first:20){ nodes{ number state repository{ nameWithOwner } } } } } } } } } }
```

(An organization Project reads the same way under `organization(login:)`.)
