# License considerations

## Licences per source, as the tooltip summarises them

| Source | Licence | Varies per photo? |
|---|---|---|
| Video frames (YouTube/Vimeo) | YouTube: all rights reserved unless the uploader chose CC BY. Vimeo: any Creative Commons licence. YouTube's terms also forbid downloading except through YouTube itself. | Per video |
| DuckDuckGo (and Google) images | Anything on the web, terms almost never stated, so treat as all rights reserved | Yes |
| Mountain Project | Photographer's copyright, licensed only to onX (which runs Mountain Project). Its terms allow personal, non-commercial viewing, and forbid reproduction, derivative works and scraping. | No |
| Wikimedia Commons | Mostly CC BY / CC BY-SA, some CC0 or public domain | Yes, already recorded per photo |
| Flickr (not used, as there's no API key) | Chosen per photo; the harvest asks only for CC and public-domain photos | Yes, already recorded per photo |
| OpenBeta | Photographer's copyright; get their permission | No |

## Scraping licences per photo

I sampled what's actually available before proposing anything:

- **Videos: worth doing.** yt-dlp reports a licence: for a known CC BY video it gave "Creative Commons Attribution license (reuse allowed)". It gave nothing for all 12 of the 174 beta videos I sampled, which means YouTube's standard licence.
  - **At download:** keep the metadata yt-dlp already fetches (licence, uploader, channel, title, URL) and attach it to each frame.
  - **Backfill:** one metadata-only request per video, with no re-downloading.
  - **Payoff:** every video gets a definite answer, mostly "standard licence", plus the uploader's name for credit.
- **Web images: cheap but low yield.** Only 5% of the 1,259 DuckDuckGo images in use carry any rights metadata in the file itself, and about 1% state a licence or rights URL.
  - **How:** read that metadata at download time.
  - **Backfill:** read it from the files already on disk. They're stored byte for byte, so there's nothing to re-download.
  - **Not worth it:** scraping the pages the images came from. Of 36 pages, 32 had only a site-wide "©" footer, 5 had a copyright or licence field in their structured data, and none linked a CC licence.
- **Mountain Project: nothing to scrape.** The licence is the same for every photo, and scraping photographer names would break its terms.
- **Commons and Flickr:** already recorded per photo.

## Licence considerations for non-commercial photogrammetry

This is general information about US law, not legal advice.

**Building meshes privately, not shared:**
- **Copyright:** downloading and processing photos is copying. Private, non-commercial use that turns photos into 3D geometry, with no effect on the market for the photos, has a strong fair-use argument, and in practice the risk is very low.
- **The rock itself:** its shape is a fact, not anyone's creative work. The texture is the part made of other people's pixels.
- **Site terms are a separate issue from copyright:**
  - Mountain Project forbids scraping and derivative works, and limits use to personal viewing.
  - YouTube forbids downloading except through its own features, and access by automated means.
  - The DuckDuckGo endpoint the harvest uses is unofficial.
  - Breaching these is a contract matter. The realistic consequence at this scale is being blocked, not sued.

**Publishing the meshes:** the texture carries pixels from each placed photo, so publishing it is closer to publishing an adaptation of those photos. What's allowed depends on each photo's licence:
- **CC0 / public domain:** fine.
- **CC BY:** fine with credit (author, source, licence).
- **CC BY-SA:** fine with credit, and the mesh must be shared under CC BY-SA too.
- **NC licences:** fine while the project stays non-commercial.
- **ND licences (Flickr licence ids 3 and 6):** a mesh textured from them can't be published.
- **All rights reserved** (Mountain Project, standard-licence YouTube frames, most web images): needs permission or a fair-use argument, which is weaker once published. Mountain Project's terms forbid derivative works regardless.

There's a practical way around most of this: build the geometry from every photo, since geometry is fact, but texture it only from photos you may share (your own and CC). Alternatively, publish untextured meshes. The per-mesh photo list makes either approach, and handling takedown requests, straightforward.

**Re-hosting the source photos or frames:** this is plain copying. CC photos are fine with credit and the licence. All-rights-reserved ones aren't without permission, and Mountain Project's and YouTube's terms forbid it as well. Small linked thumbnails have some precedent as fair use, but only in a search-engine context.

**Linking to sources:** linking to the source pages, and listing which photos or frames each mesh used, is fine. It's also how CC attribution is meant to work. Avoid hotlinking images from other sites' servers: US courts disagree on embedding, and Mountain Project's terms forbid using its content.

Sources:
- [Adventure Projects (Mountain Project) terms](https://www.adventureprojects.net/ap-terms)
- [Gripped: Mountain Project replies to OpenBeta copyright issues](https://gripped.com/?p=39886)
- [EFF on the OpenBeta copyright claims](https://www.eff.org/deeplinks/2021/03/free-climbing-rock-climbers-open-data-project-threatened-bogus-copyright-claims)
- [OpenBeta: Share your climbing photos](https://openbeta.substack.com/p/share-your-climbing-photos)
- [YouTube Terms of Service](https://www.youtube.com/static?template=terms)
- [YouTube Standard vs Creative Commons licence](https://vondranlegal.com/what-is-a-youtube-standard-license-vs-creative-commons-attribution-sharing-videos-legal/)
- [University of Waterloo on downloading YouTube videos](https://uwaterloo.ca/copyright-at-waterloo/faq-0-9)
