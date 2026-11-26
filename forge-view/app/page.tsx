import { redirect } from "next/navigation";

// The pages live under /demo, the same paths the live site serves.
export default function Home() {
  redirect("/demo");
}
